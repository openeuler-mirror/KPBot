#!/usr/bin/env python3
"""kptools-ascend-profiling-compare 的公共读取与度量层（kpc_core）。

供对比体系其余 skill 复用（引用前提：本 skill 已合入 master）：
- 采集定位与侧车读取：locate_capture / load_capture / _read_meta / _detect_has_stack
- 侧车解析：_read_step_metrics_native / _read_step_time_db / _read_kernels_csv /
  _read_comm_json / _read_api_csv
- trace-only 路径：_index_trace / _prune_cache / _classes_from_db / _steps_from_db /
  _events_kernels / _metrics_from_classes / _load_trace_only
- 区间与统计：_union / _cover / _complement / _subtract / _median / _mad / _mean / _round
- 对比度量：METRIC_DEFS / _metric_block / _verdict / _dominant / _pct_median /
  _collapse / _find_step / _pick_steps / _kernel_avg / _class_family / _norm_kernel_name

时间在内部统一保持 µs，展示层再换算 ms。
"""

import csv
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
SKILLS_DIR = SKILL_DIR.parent
CACHE_DIR = Path(os.path.expanduser("~/.cache/kpbot_profiling_compare"))

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}

# 通信算子名带每次采集都不同的任务下标（hcom_allReduce__503_0_1），
# 直接按原名比较会把同一算子当成两个不同算子。
_HCOM_INSTANCE = re.compile(r"^(hcom_[A-Za-z0-9]+?)__\d+(?:_\d+)*$")

_MODULE_CACHE = {}
# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _norm_kernel_name(name):
    """把通信算子的实例后缀归一化，保证两次采集可比。"""
    m = _HCOM_INSTANCE.match(name)
    return m.group(1) if m else name


def _round(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def _median(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _mad(xs):
    if not xs:
        return 0.0
    m = _median(xs)
    return _median([abs(x - m) for x in xs])


def _union(ivs):
    out = []
    for a, b in sorted((float(s), float(e)) for s, e in ivs if e > s):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _cover(ivs, a, b):
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in _union(ivs))


def _complement(ivs, a, b):
    out, prev = [], a
    for s, e in _union(ivs):
        if s > prev:
            out.append((prev, min(s, b)))
        prev = max(prev, e)
        if prev >= b:
            break
    if prev < b:
        out.append((prev, b))
    return out


def _subtract(ivs, other):
    """返回 ivs 中不与 other 重叠的区间。"""
    out = []
    other = _union(other)
    for s, e in _union(ivs):
        cur = s
        for os_, oe in other:
            if oe <= cur:
                continue
            if os_ >= e:
                break
            if os_ > cur:
                out.append((cur, min(os_, e)))
            cur = max(cur, oe)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def _skill_script(skill_name, rel_path):
    """按 skill 名找脚本，兼容 skills/<name>/ 与 skills/<domain>/<name>/ 两种布局。"""
    direct = SKILLS_DIR / skill_name / rel_path
    if direct.exists():
        return direct
    for pattern in (f"*/{skill_name}/{rel_path}", f"*/*/{skill_name}/{rel_path}"):
        hits = sorted(SKILLS_DIR.glob(pattern))
        if hits:
            return hits[0]
    return direct


def _load_module(name, rel_path):
    if name in _MODULE_CACHE:
        return _MODULE_CACHE[name]
    skill_name, sub_path = rel_path.split("/", 1)
    path = _skill_script(skill_name, sub_path)
    if not path.exists():
        raise FileNotFoundError(f"缺少依赖脚本: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MODULE_CACHE[name] = mod
    return mod


def _nostack_module():
    return _load_module(
        "kpc_nostack_detect",
        "kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py",
    )


def _nostack_classes_module():
    return _load_module(
        "kpc_nostack_analysis",
        "kptools-ascend-profiling-analysis-nostack/scripts/nostack_analysis.py",
    )

# ---------------------------------------------------------------------------
# 采集定位与侧车读取
# ---------------------------------------------------------------------------
def locate_capture(path):
    """返回 (output_dir, profiler_root, trace_path)。"""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        if p.name == "trace_view.json":
            return p.parent, p.parent.parent, p
        return p.parent, p.parent.parent, p.parent / "trace_view.json"
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")

    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        return direct, p, direct / "trace_view.json"
    if (p / "analysis.db").exists() or (p / "step_trace_time.csv").exists() or (
        p / "trace_view.json"
    ).exists():
        return p, p.parent, p / "trace_view.json"

    outs = sorted(p.rglob("ASCEND_PROFILER_OUTPUT"), key=lambda x: len(x.parts))
    if outs:
        return outs[0], outs[0].parent, outs[0] / "trace_view.json"
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if traces:
        return traces[0].parent, traces[0].parent.parent, traces[0]
    return p, p.parent, p / "trace_view.json"


def _read_meta(output_dir, root):
    meta = {
        "version": None,
        "cann_version": None,
        "rank": None,
        "has_stack": None,
        "profiler_info": None,
    }
    candidates = sorted(output_dir.glob("profiler_info_*.json")) + sorted(
        root.glob("profiler_info_*.json")
    )
    for f in candidates:
        try:
            with open(f, encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception:
            continue
        meta["profiler_info"] = str(f)
        meta["version"] = j.get("torch_npu_version")
        meta["cann_version"] = j.get("cann_version")
        meta["rank"] = j.get("rank_id")
        cfg = (j.get("config") or {}).get("common_config") or {}
        meta["has_stack"] = cfg.get("with_stack")
        break
    return meta

def _detect_has_stack(trace_path):
    """按 trace 内是否真有 python_function 事件判定有栈，比 profiler_info 的开关可靠。"""
    p = Path(trace_path)
    if not p.is_file():
        return None
    marker = b'"python_function"'
    try:
        with open(p, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return False
                if marker in tail + chunk:
                    return True
                tail = chunk[-len(marker):]
    except OSError:
        return None


def _read_step_metrics_native(output_dir):
    """返回 ({step_id: metrics_µs}, source, multi_device_note)。

    step_trace_time 侧车按 deviceId 分行。单 device 时直接聚合；多 device 时
    按 step 取 Stage 最大的卡作为主卡口径（只保留该卡分量），保证
    Computing + Communication(未重叠) + Free + Preparing = 步耗时 的可加性。
    """
    adb = output_dir / "analysis.db"
    rows = []
    source = None
    if adb.exists():
        try:
            con = sqlite3.connect(f"file:{adb}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT deviceId,step,computing,communication_not_overlapped,"
                "communication,free,stage,preparing FROM StepTraceTime ORDER BY step"
            ).fetchall()
            con.close()
            source = "analysis.db:StepTraceTime"
        except Exception:
            rows = []
    if not rows:
        csv_path = output_dir / "step_trace_time.csv"
        if csv_path.exists():
            with open(csv_path, encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append(
                            (
                                r.get("Device_id", ""),
                                int((r.get("Step") or "").strip()),
                                float(r.get("Computing") or 0),
                                float(r.get("Communication(Not Overlapped)") or 0),
                                float(r.get("Communication") or 0),
                                float(r.get("Free") or 0),
                                float(r.get("Stage") or 0),
                                float(r.get("Preparing") or 0),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            if rows:
                source = "step_trace_time.csv"

    per_step = {}
    devices = set()
    for dev, sid, comp, cno, comm, free, stage, prep in rows:
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        if dev not in (None, ""):
            devices.add(str(dev))
        per_step.setdefault(sid, []).append(
            {
                "dev": str(dev),
                "comp": float(comp),
                "comm_no": float(cno),
                "comm": float(comm),
                "free": float(free),
                "stage": float(stage),
                "prep": float(prep),
            }
        )

    note = None
    agg = {}
    if len(devices) > 1:
        # 多 deviceId：每 step 取 Stage 最大的卡作为主卡，只保留该卡的分量。
        # 求和 Computing 而 Stage 取 max 会破坏可加性（Comp+Comm+Free+Prep ≠ 步耗时），
        # 使占比超过 100%、主导分量判定失真，因此改为单卡口径。
        for sid, items in per_step.items():
            m = max(items, key=lambda x: (x["stage"], x["comp"], x["dev"]))
            m = dict(m)
            m["free"] = max(0.0, m["stage"] - m["comp"] - m["comm_no"])
            agg[sid] = m
        note = (
            f"检测到 {len(devices)} 个 Device_id：按 step 取 Stage 最大的卡作为主卡口径，"
            "分量仅统计该卡（保证可加性），其余卡的耗时未纳入分量对比"
        )
    else:
        for sid, items in per_step.items():
            m = {"comp": 0.0, "comm_no": 0.0, "comm": 0.0, "free": 0.0,
                 "stage": 0.0, "prep": 0.0}
            for it in items:
                m["comp"] += it["comp"]
                m["comm_no"] += it["comm_no"]
                m["comm"] += it["comm"]
                m["free"] += it["free"]
                m["stage"] = max(m["stage"], it["stage"])
                m["prep"] = max(m["prep"], it["prep"])
            agg[sid] = m
    return agg, source, note


def _add_sidecar(bucket, name):
    if name and name not in bucket:
        bucket.append(name)


def _read_step_time_db(output_dir):
    best = {}
    best_db = None
    for db in sorted(output_dir.glob("ascend_pytorch_profiler_*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT id,startNs,endNs FROM STEP_TIME ORDER BY id").fetchall()
            con.close()
            rows = [(int(i), int(s), int(e)) for i, s, e in rows if e > s]
        except Exception:
            continue
        if len(rows) > len(best):
            best = {i: (s, e) for i, s, e in rows}
            best_db = db.name
    return best, best_db


def _read_kernels_csv(output_dir):
    path = output_dir / "kernel_details.csv"
    if not path.exists():
        return {}, None
    agg = defaultdict(lambda: defaultdict(lambda: {"dur": 0.0, "count": 0, "type": ""}))
    with open(path, encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            try:
                sid = int((r.get("Step Id") or "").strip())
                name = _norm_kernel_name((r.get("Name") or "").strip())[:160]
                dur = float(r.get("Duration(us)") or 0)
                typ = (r.get("Type") or "").strip()
            except (TypeError, ValueError):
                continue
            if not name or dur <= 0:
                continue
            slot = agg[sid][name]
            slot["dur"] += dur
            slot["count"] += 1
            if typ and not slot["type"]:
                slot["type"] = typ
    return {sid: dict(v) for sid, v in agg.items()}, "kernel_details.csv"


def _comm_time(info):
    if not isinstance(info, dict):
        return None
    ti = info.get("Communication Time Info")
    if not isinstance(ti, dict) or not ti:
        return None
    try:
        return (
            float(ti.get("Elapse Time(ms)") or 0),
            float(ti.get("Wait Time(ms)") or 0),
            float(ti.get("Transit Time(ms)") or 0),
            float(ti.get("Synchronization Time(ms)") or 0),
        )
    except (TypeError, ValueError):
        return None


def _read_comm_json_file(output_dir):
    path = output_dir / "communication.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    out = {}
    for key, grp in data.items():
        m = re.match(r"^step(\d+)$", str(key))
        if not m:
            continue
        sid = int(m.group(1))
        acc = {"elapse": 0.0, "wait": 0.0, "transit": 0.0, "sync": 0.0}
        for kind in ("collective", "p2p"):
            ops = grp.get(kind) or {}
            total = _comm_time(ops.get("Total Op Info"))
            if total is not None:
                acc["elapse"] += total[0]
                acc["wait"] += total[1]
                acc["transit"] += total[2]
                acc["sync"] += total[3]
            else:
                for name, info in ops.items():
                    vals = _comm_time(info)
                    if vals is None:
                        continue
                    acc["elapse"] += vals[0]
                    acc["wait"] += vals[1]
                    acc["transit"] += vals[2]
                    acc["sync"] += vals[3]
        out[sid] = acc
    return out


def _read_comm_db(output_dir):
    """analysis.db:CommAnalyzerTime 兜底（communication.json 缺失时）。

    表按通信算子分行，单位 ms；按 step 汇总 elapse/wait/transit/sync，
    与 communication.json 的 step 口径一致。
    """
    adb = output_dir / "analysis.db"
    if not adb.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{adb}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT step, elapse_time, wait_time, transit_time, synchronization_time "
            "FROM CommAnalyzerTime"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for step, elapse, wait, transit, sync in rows:
        m = re.match(r"^step(\d+)$", str(step))
        if not m:
            continue
        sid = int(m.group(1))
        acc = out.setdefault(sid, {"elapse": 0.0, "wait": 0.0, "transit": 0.0, "sync": 0.0})
        try:
            acc["elapse"] += float(elapse or 0)
            acc["wait"] += float(wait or 0)
            acc["transit"] += float(transit or 0)
            acc["sync"] += float(sync or 0)
        except (TypeError, ValueError):
            continue
    return out


def _read_comm(output_dir):
    """返回 (data, source)。communication.json 优先，缺失时读 CommAnalyzerTime。"""
    data = _read_comm_json_file(output_dir)
    if data:
        return data, "communication.json"
    data = _read_comm_db(output_dir)
    if data:
        return data, "analysis.db:CommAnalyzerTime"
    return {}, None


def _read_comm_json(output_dir):
    """兼容入口：只返回通信数据。"""
    return _read_comm(output_dir)[0]


def _read_api_csv(output_dir):
    for name in ("api_detail.csv", "api_statistic.csv"):
        path = output_dir / name
        if not path.exists():
            continue
        agg = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    api = (r.get("API Name") or r.get("Name") or "").strip()
                    if not api:
                        continue
                    t = float(r.get("Time(us)") or 0)
                    c = int(float(r.get("Count") or 0))
                except (TypeError, ValueError):
                    continue
                slot = agg.setdefault(api, {"time": 0.0, "count": 0})
                slot["time"] += t
                slot["count"] += c
        if agg:
            return agg, name
    return {}, None

# ---------------------------------------------------------------------------
# trace-only：复用已有 skill
# ---------------------------------------------------------------------------
CACHE_SCHEMA = "v2"
CACHE_KEEP = 32


def _prune_cache(keep=CACHE_KEEP):
    """索引库可随时重建，按 mtime 只保留最近若干个，避免无界增长。"""
    try:
        files = sorted(CACHE_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _index_trace(trace_path):
    """用 analysis skill 的 build_index.py 建索引，返回 (db_path, error)。"""
    build_index = _skill_script("kptools-ascend-profiling-analysis", "scripts/build_index.py")
    if not build_index.exists():
        return None, f"缺少 {build_index}"
    st = os.stat(trace_path)
    bi = os.stat(build_index)
    # 键里带上 build_index 自身指纹与 schema 版本，升级依赖脚本后不会命中旧索引。
    key = hashlib.sha1(
        f"{os.path.abspath(trace_path)}|{st.st_size}|{int(st.st_mtime)}"
        f"|{bi.st_size}|{int(bi.st_mtime)}|{CACHE_SCHEMA}".encode()
    ).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{key}.db"
    if out.exists() and out.stat().st_size > 0:
        try:
            os.utime(out, None)
        except OSError:
            pass
        _prune_cache()
        return str(out), None
    try:
        run = subprocess.run(
            [sys.executable, str(build_index), str(trace_path), "-o", str(out)],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return None, "build_index 超时"
    if run.returncode != 0 or not out.exists():
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-3:]
        return None, "build_index 失败: " + " | ".join(tail)
    _prune_cache()
    return str(out), None


def _classes_from_db(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute("SELECT kind,start_ns,end_ns FROM classes").fetchall()
    try:
        src = con.execute("SELECT value FROM meta WHERE key='classification_source'").fetchone()
        src = src[0] if src else "build_index"
    except Exception:
        src = "build_index"
    con.close()
    groups = defaultdict(list)
    for kind, s, e in rows:
        groups[kind].append((int(s), int(e)))
    return groups, src


def _steps_from_db(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id,start_ns,end_ns FROM steps ORDER BY id").fetchall()
    except Exception:
        rows = []
    con.close()
    return [(int(i), int(s), int(e)) for i, s, e in rows if e > s]


def _events_kernels(events, steps):
    """从 trace 事件聚合 device kernel；steps 为 [(id,start,end)]。"""
    starts = [s for _, s, _ in steps]

    def sid_of(ts, dur):
        if not steps:
            return 0, dur
        idx = max(0, min(len(starts) - 1, _bisect(starts, ts) - 1))
        sid, a, b = steps[idx]
        if ts < a or ts >= b:
            return None, 0.0
        # 跨步边界的 kernel：按本条 step 内部分计时，避免把别步时长算进来。
        return sid, min(dur, b - ts)

    agg = defaultdict(lambda: defaultdict(lambda: {"dur": 0.0, "count": 0, "type": ""}))
    for ts, dur, name, cat, pid, tid in events:
        if not dur or dur <= 0:
            continue
        if not (cat in AI_TASK_TYPES or name.startswith("hcom")):
            continue
        sid, clipped = sid_of(ts, dur)
        if sid is None:
            continue
        slot = agg[sid][_norm_kernel_name(name)[:160]]
        slot["dur"] += clipped / 1000.0
        slot["count"] += 1
        slot["type"] = cat or slot["type"]
    return {sid: dict(v) for sid, v in agg.items()}


def _bisect(arr, x):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _metrics_from_classes(groups, step):
    _sid, a, b = step
    dur = (b - a) / 1000.0
    comp_ivs = groups.get("computing", [])
    comm_ivs = groups.get("communication", [])
    comp = _cover(comp_ivs, a, b) / 1000.0
    comm = _cover(comm_ivs, a, b) / 1000.0
    if "comm_not_overlapped" in groups:
        cno_ivs = groups["comm_not_overlapped"]
    else:
        cno_ivs = _subtract(comm_ivs, comp_ivs)
    cno = _cover(cno_ivs, a, b) / 1000.0
    busy = list(comp_ivs) + list(comm_ivs)
    busy_us = _cover(busy, a, b) / 1000.0
    gaps = _complement(busy, a, b)
    prep = 0.0
    # 无任何忙碌区间时无法区分 Preparing 与 Free，按 Free 计，避免伪造 100% Preparing。
    if busy and gaps and gaps[0][0] <= a + 1:
        prep = (gaps[0][1] - gaps[0][0]) / 1000.0
    free = max(0.0, dur - busy_us - prep)
    return {
        "dur": dur,
        "comp": comp,
        "comm_no": cno,
        "comm": comm,
        "free": free,
        "prep": prep,
    }


def _load_trace_only(output_dir, root, trace_path, notes, degradations):
    trace_path = Path(trace_path)
    if not trace_path.exists():
        return None
    sidecars = []
    _add_sidecar(sidecars, trace_path.name)
    # trace 事件解析与自建分类依赖 nostack skill；缺失时仍可用 build_index 侧车出报告。
    ns = None
    try:
        ns = _nostack_module()
    except (FileNotFoundError, ImportError) as exc:
        notes.append(f"缺少 trace 解析依赖: {exc}")
        degradations.append(
            "缺少 kptools-ascend-profiling-analysis-nostack 依赖，"
            "trace 事件解析与自建四分类不可用"
        )
    events = []
    if ns is not None:
        events, _host_pid, _ui_zero = ns.load_trace(str(trace_path))
        has_stack = any(e[3] == "python_function" for e in events)
    else:
        has_stack = _detect_has_stack(trace_path)

    db, err = _index_trace(trace_path)
    if err:
        notes.append(err)
        degradations.append(f"trace 索引不可用: {err}")

    groups = {}
    class_source = "self_built"
    raw_steps = []
    steps_origin = "detect_steps"
    if db:
        try:
            groups, class_source = _classes_from_db(db)
            raw_steps = _steps_from_db(db)
            if raw_steps:
                steps_origin = "build_index"
        except Exception as exc:
            notes.append(f"读取 build_index 失败: {exc}")
            degradations.append(f"读取 build_index 失败: {exc}")

    if not raw_steps and ns is not None:
        try:
            res = ns.detect(str(trace_path))
            raw_steps = [(s["id"], s["start_ns"], s["end_ns"]) for s in res["steps"]]
            notes.append(
                "step 边界来源: detect_steps "
                f"({res.get('source')}/{res.get('confidence')})"
            )
            degradations.append("无权威 step 标识，退化为 detect_steps 切分")
            if res.get("notes"):
                notes.extend(res["notes"][:4])
        except Exception as exc:
            notes.append(f"detect_steps 失败: {exc}")
            degradations.append("detect_steps 切分失败，未切出 step")
    elif raw_steps:
        notes.append("step 边界来源: build_index")

    if not groups:
        try:
            na = _nostack_classes_module()
            comp, comm, cno, class_source = na.resolve_classes(events)
            groups = {
                "computing": comp,
                "communication": comm,
                "comm_not_overlapped": cno,
            }
        except Exception as exc:
            notes.append(f"分类回退失败: {exc}")
            degradations.append("四分类缺失，未能自建设备任务分类")
    if not (groups.get("computing") or groups.get("communication")):
        class_source = "none"
        notes.append("未获得设备任务分类（无 Overlap Analysis / 设备任务类型）")
        degradations.append("未获得设备任务分类，Computing/Communication/Free/Preparing 仅展示数值")

    whole_window = False
    if not raw_steps and events:
        lo = min(e[0] for e in events)
        hi = max(e[0] + e[1] for e in events if e[1] > 0) if any(e[1] > 0 for e in events) else lo
        raw_steps = [(0, lo, hi)]
        whole_window = True
        steps_origin = "window"
        notes.append("未切出 step，退化为整窗对比")
        degradations.append("未切出 step，退化为整窗对比")
    if not raw_steps:
        return None

    steps = []
    for sid, a, b in sorted(raw_steps):
        m = _metrics_from_classes(groups, (sid, a, b))
        steps.append(
            {
                "id": sid,
                "start_ns": a,
                "end_ns": b,
                "dur": m["dur"],
                "comp": m["comp"],
                "comm_no": m["comm_no"],
                "comm": m["comm"],
                "free": m["free"],
                "prep": m["prep"],
            }
        )

    kernels, ksrc = _read_kernels_csv(output_dir)
    if kernels:
        _add_sidecar(sidecars, ksrc)
    else:
        kernels = _events_kernels(events, raw_steps)
        ksrc = "trace_view.json"
    comm, csrc = _read_comm(output_dir)
    _add_sidecar(sidecars, csrc)
    api, asrc = _read_api_csv(output_dir)
    if asrc:
        _add_sidecar(sidecars, asrc)
    meta = _read_meta(output_dir, root)
    if meta["profiler_info"]:
        _add_sidecar(sidecars, Path(meta["profiler_info"]).name)
    meta["has_stack"] = has_stack

    return {
        "path": str(trace_path),
        "kind": "trace_view",
        "step_source": steps_origin,
        "class_source": class_source,
        "kernel_source": ksrc,
        "api_source": asrc,
        "steps": steps,
        "whole_window": whole_window,
        "kernels": kernels,
        "comm": comm,
        "has_comm": bool(comm),
        "api": api,
        "window": (min(s["start_ns"] for s in steps), max(s["end_ns"] for s in steps)),
        "notes": notes,
        "sidecars": sidecars,
        "degradations": degradations,
        **{k: meta[k] for k in ("version", "cann_version", "rank", "has_stack")},
    }


# ---------------------------------------------------------------------------
# 采集加载
# ---------------------------------------------------------------------------
def load_capture(path, label=None):
    notes = []
    degradations = []
    sidecars = []
    output_dir, root, trace_path = locate_capture(path)
    metrics, step_source, multi_dev_note = _read_step_metrics_native(output_dir)
    if multi_dev_note:
        notes.append(multi_dev_note)

    if not metrics:
        if not Path(trace_path).exists():
            # 仅给 db 时：STEP_TIME 仍可提供步跨度，四分类不可用。
            st, st_name = _read_step_time_db(output_dir)
            if st:
                steps = [
                    {"id": sid, "start_ns": a, "end_ns": b, "dur": (b - a) / 1000.0,
                     "comp": 0.0, "comm_no": 0.0, "comm": 0.0, "free": 0.0, "prep": 0.0}
                    for sid, (a, b) in sorted(st.items())
                ]
                _add_sidecar(sidecars, st_name)
                meta = _read_meta(output_dir, root)
                degradations.append("仅有 STEP_TIME：可比较步跨度，四分类不可用")
                return {
                    "label": label or Path(path).name, "path": str(output_dir),
                    "kind": "native", "step_source": "STEP_TIME",
                    "class_source": "none", "kernel_source": "none",
                    "steps": steps, "whole_window": False, "kernels": {}, "comm": {},
                    "has_comm": False, "api": {},
                    "window": (steps[0]["start_ns"], steps[-1]["end_ns"]),
                    "notes": notes, "sidecars": sidecars, "degradations": degradations,
                    "version": meta["version"], "cann_version": meta["cann_version"],
                    "rank": meta["rank"], "has_stack": None,
                }
        degradations.append("缺少 StepTraceTime 侧车，回退 trace_view.json 路径")
        cap = _load_trace_only(output_dir, root, trace_path, notes, degradations)
        if cap is None:
            raise FileNotFoundError(
                f"无法从 {path} 读取 profiling 数据（{'；'.join(degradations) or '无降级信息'}）"
            )
        cap["label"] = label or Path(path).name
        return cap

    step_time, step_time_db = _read_step_time_db(output_dir)
    _add_sidecar(sidecars, (step_source or "").split(":")[0] or None)
    _add_sidecar(sidecars, step_time_db)
    steps = []
    span_mismatch = 0
    for sid in sorted(metrics):
        m = metrics[sid]
        start, end = step_time.get(sid, (None, None))
        prep = m["prep"]
        # StepTraceTime 的 stage 只覆盖设备活跃窗口，端到端步耗时 = stage + preparing；
        # 实测与 STEP_TIME 跨度一致（误差 < 1%）。
        dur = m["stage"] + prep
        if dur <= 0:
            dur = (end - start) / 1000.0 if end else 0.0
        elif end:
            span = (end - start) / 1000.0
            if span > 0 and abs(dur - span) / span > 0.02:
                span_mismatch += 1
        steps.append(
            {
                "id": sid,
                "start_ns": start,
                "end_ns": end,
                "dur": dur,
                "comp": m["comp"],
                "comm_no": m["comm_no"],
                "comm": m["comm"],
                "free": m["free"],
                "prep": prep,
            }
        )
    if span_mismatch:
        notes.append(
            f"{span_mismatch} 步的 stage+preparing 与 STEP_TIME 跨度差异超过 2%，"
            "步时间口径可能不一致"
        )

    kernels, ksrc = _read_kernels_csv(output_dir)
    _add_sidecar(sidecars, "kernel_details.csv" if ksrc else None)
    comm, csrc = _read_comm(output_dir)
    _add_sidecar(sidecars, csrc)
    api, asrc = _read_api_csv(output_dir)
    _add_sidecar(sidecars, asrc)
    meta = _read_meta(output_dir, root)
    if meta["profiler_info"]:
        _add_sidecar(sidecars, Path(meta["profiler_info"]).name)
    detected_stack = _detect_has_stack(trace_path)
    if detected_stack is not None:
        meta["has_stack"] = detected_stack

    window = (
        (min(s["start_ns"] for s in steps if s["start_ns"] is not None),
         max(s["end_ns"] for s in steps if s["end_ns"] is not None))
        if any(s["start_ns"] is not None for s in steps)
        else (None, None)
    )
    return {
        "label": label or Path(path).name,
        "path": str(output_dir),
        "kind": "native",
        "step_source": step_source or "STEP_TIME",
        "class_source": step_source or "CANN",
        "kernel_source": ksrc,
        "api_source": asrc,
        "steps": steps,
        "whole_window": False,
        "kernels": kernels,
        "comm": comm,
        "has_comm": bool(comm),
        "api": api,
        "window": window,
        "notes": notes,
        "sidecars": sidecars,
        "degradations": degradations,
        **{k: meta[k] for k in ("version", "cann_version", "rank", "has_stack")},
    }

# ---------------------------------------------------------------------------
# 对比基础度量
# ---------------------------------------------------------------------------
METRIC_DEFS = (
    ("dur", "步耗时"),
    ("comp", "Computing"),
    ("comm_no", "Communication(未重叠)"),
    ("free", "Free"),
    ("prep", "Preparing"),
)

# 四分类来源族：CANN 侧车（analysis.db / step_trace_time.csv）算一族，
# trace 自建（Overlap Analysis / 设备任务类型）算另一族。跨族的分量拆分不可比。
CLASS_FAMILY = {"native": "cann", "trace_view": "trace"}


def _class_family(cap):
    return CLASS_FAMILY.get(cap.get("kind"), "trace")


def _collapse(cap):
    steps = cap["steps"]
    n = len(steps) or 1
    total = {"id": 0, "start_ns": cap["window"][0], "end_ns": cap["window"][1], "dur": 0.0,
             "comp": 0.0, "comm_no": 0.0, "comm": 0.0, "free": 0.0, "prep": 0.0}
    for s in steps:
        for k in ("dur", "comp", "comm_no", "comm", "free", "prep"):
            total[k] += s[k]
    kernels = defaultdict(lambda: {"dur": 0.0, "count": 0, "type": ""})
    for s in steps:
        for name, v in cap["kernels"].get(s["id"], {}).items():
            kernels[name]["dur"] += v["dur"]
            kernels[name]["count"] += v["count"]
            kernels[name]["type"] = v["type"] or kernels[name]["type"]
    comm = defaultdict(float)
    for s in steps:
        c = cap["comm"].get(s["id"])
        if c:
            for k in ("elapse", "wait", "transit", "sync"):
                comm[k] += c[k]
    has_comm = bool(cap.get("has_comm")) or bool(cap.get("comm"))
    out = dict(cap)
    out["steps"] = [total]
    out["kernels"] = {0: dict(kernels)}
    # 没有通信侧车时不能伪造零值 comm[0]，否则消费侧会把“缺失”当成“通信为 0”。
    out["comm"] = (
        {0: {"elapse": comm["elapse"], "wait": comm["wait"],
             "transit": comm["transit"], "sync": comm["sync"]}}
        if has_comm else {}
    )
    out["has_comm"] = has_comm
    out["_collapsed_from"] = n
    return out


def _find_step(cap, sid):
    for s in cap["steps"]:
        if s["id"] == sid:
            return s
    raise KeyError(f"{cap['label']} 无 step{sid}，可用: {[s['id'] for s in cap['steps']][:20]}")


def _pick_steps(cap, ids):
    m = {s["id"]: s for s in cap["steps"]}
    missing = [i for i in ids if i not in m]
    if missing:
        raise KeyError(f"{cap['label']} 无 step{missing}，可用: {sorted(m)[:20]}")
    return [m[i] for i in ids]


def _series(steps, key):
    return [float(s[key]) for s in steps]


def _verdict(delta_ms, noise_ms):
    # delta = base - cand，正值表示候选更省
    if delta_ms > noise_ms:
        return "改善"
    if delta_ms < -noise_ms:
        return "恶化"
    return "在噪声内"


def _metric_block(base_steps, cand_steps, key, min_delta_ms, min_delta_pct=1.0):
    """用**逐对差值**算 Δ 与噪声带。

    先按 step 配对（已经对齐），对每对求 base−cand，再取中位数与 MAD。
    这样消除了 workload 本身的步间方差，少量步也能识别一致性改善/退化；
    步间方向不一致时单独裁决，避免把双峰采集误判成“在噪声内”。
    噪声带同时受绝对下限与相对下限约束，避免把 0.4% 的差异当成收益。
    """
    b = [x / 1000.0 for x in _series(base_steps, key)]
    c = [x / 1000.0 for x in _series(cand_steps, key)]
    n = min(len(b), len(c))
    paired = [b[i] - c[i] for i in range(n)]
    bm, cm = _median(b), _median(c)
    delta = bm - cm
    dmed = _median(paired)
    noise = max(min_delta_ms, abs(bm) * min_delta_pct / 100.0, _mad(paired))
    pos = sum(1 for d in paired if d > 1e-9)
    neg = sum(1 for d in paired if d < -1e-9)
    nonzero = pos + neg
    consistency = (max(pos, neg) / nonzero) if nonzero else 1.0
    heterogeneous = nonzero >= 2 and consistency < 0.75
    if heterogeneous:
        verdict = "方向不一致"
    else:
        verdict = _verdict(dmed, noise)
    pct = (delta / bm * 100.0) if bm else 0.0
    return {
        "base_median_ms": _round(bm),
        "cand_median_ms": _round(cm),
        "base_mean_ms": _round(_mean(b)),
        "cand_mean_ms": _round(_mean(c)),
        "delta_ms": _round(delta),
        "paired_delta_ms": _round(dmed),
        "pct": _round(pct, 2),
        "noise_ms": _round(noise),
        "verdict": verdict,
        "n_improved": pos,
        "n_regressed": neg,
        "sign_consistency": _round(consistency, 3),
        "heterogeneous": heterogeneous,
    }


def _pct_median(steps, key):
    out = []
    for s in steps:
        if s["dur"] > 0:
            out.append(s[key] / s["dur"] * 100.0)
    return _median(out)


def _dominant(steps):
    # 占比分母是步耗时（含 Preparing），因此候选必须包含 Preparing，
    # 否则 prefill 等大 Preparing 场景会把主导分量误判成 Computing/Free。
    pcts = {label: _pct_median(steps, key) for key, label in
            (("comp", "Computing"), ("comm_no", "Communication"),
             ("free", "Free"), ("prep", "Preparing"))}
    return max(pcts, key=pcts.get), pcts


def _kernel_avg(cap, steps):
    n = len(steps) or 1
    agg = {}
    for s in steps:
        for name, v in cap["kernels"].get(s["id"], {}).items():
            slot = agg.setdefault(name, {"dur": 0.0, "count": 0, "type": ""})
            slot["dur"] += v["dur"]
            slot["count"] += v["count"]
            slot["type"] = v["type"] or slot["type"]
    for v in agg.values():
        v["dur"] /= n
    return agg
