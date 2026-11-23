#!/usr/bin/env python3
"""算子统计对比（opstats）：两份 Ascend profiling 的算子种类/次数/shape/分位数对比。

输出 SKILL_DEV_PLAN 附录 A.1 统一信封（9 字段）+ HTML 报告。
数据源优先级：kernel_details.csv → ascend_pytorch_profiler_*.db:TASK → trace_view.json 设备事件。

用法:
  opstats.py <base> <cand> [--out-html r.html] [--json r.json]
             [--min-delta-ms 0.05] [--min-delta-count 0] [--rename-jaccard 0.6]
"""

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
SCHEMA_VERSION = 1
# trace 全量 json.loads 的内存约为文件体积的数倍；超阈值时只解析前 N MB 的完整事件。
MAX_TRACE_BYTES = 512 * 1024 * 1024
TRACE_TRUNCATE_BYTES = 128 * 1024 * 1024

SHAPE_KEYS = ("shape", "input shape", "input shapes", "output shape", "output shapes")

# 通信算子名带每次采集/每个任务都不同的实例下标（hcom_allReduce__503_98_1），
# 不归一化会把同一类别算成多个 op_kind。口径与 kptools-ascend-profiling-compare 一致。
_HCOM_INSTANCE = re.compile(
    r"^(hcom_[A-Za-z][A-Za-z0-9]*?)(?:(?:__|_)\d+(?:_\d+)*)$"
)


def norm_name(name):
    m = _HCOM_INSTANCE.match(str(name))
    return m.group(1) if m else str(name)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def round4(x, n=4):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


def median(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def mad(xs):
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return 0.0
    m = median(xs)
    return median([abs(x - m) for x in xs])


def percentile(xs, p):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def tokens(name):
    n = re.sub(r"[^A-Za-z0-9]+", " ", str(name))
    n = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", n)
    out = set()
    for t in n.lower().split():
        t = re.sub(r"\d+$", "", t)
        if t and not t.isdigit():
            out.add(t)
    return out


def similar_name(a, b, threshold):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    union = len(ta | tb)
    return union > 0 and inter / union >= threshold


# ---------------------------------------------------------------------------
# 采集定位与数据读取
# ---------------------------------------------------------------------------
def locate(path):
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    if p.is_file():
        return (p.parent, p) if p.name == "trace_view.json" else (p.parent, p.parent / "trace_view.json")
    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        return direct, direct / "trace_view.json"
    if (p / "kernel_details.csv").exists() or (p / "analysis.db").exists() or (p / "trace_view.json").exists():
        return p, p / "trace_view.json"
    outs = sorted(p.rglob("ASCEND_PROFILER_OUTPUT"), key=lambda x: len(x.parts))
    if outs:
        return outs[0], outs[0] / "trace_view.json"
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if traces:
        return traces[0].parent, traces[0]
    return p, p / "trace_view.json"


def _norm_header(h):
    return re.sub(r"[\s_()]+", " ", str(h).strip().lower()).strip()


def _pick_col(fields, candidates):
    cols = _pick_cols(fields, candidates)
    return cols[0] if cols else None


def _pick_cols(fields, candidates):
    """返回所有匹配列（精确名优先，其次子串匹配），用于 Input/Output Shapes 合并。"""
    norm = {_norm_header(f): f for f in fields}
    out = []
    for cand in candidates:
        key = _norm_header(cand)
        if key in norm and norm[key] not in out:
            out.append(norm[key])
    for key, orig in norm.items():
        if orig in out:
            continue
        if any(cand in key for cand in candidates):
            out.append(orig)
    return out


def read_csv(output_dir, notes, degradations):
    path = output_dir / "kernel_details.csv"
    if not path.exists():
        return None
    rows = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError as exc:
        degradations.append(f"kernel_details.csv 读取失败（{type(exc).__name__}: {exc}）")
        return None
    with fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            degradations.append("kernel_details.csv 解析失败（表头为空）")
            return None
        c_step = _pick_col(reader.fieldnames, ("step id", "step"))
        c_name = _pick_col(reader.fieldnames, ("name", "op name"))
        c_dur = _pick_col(reader.fieldnames, ("duration(us)", "duration us", "duration"))
        c_type = _pick_col(reader.fieldnames, ("type", "task type"))
        c_shapes = _pick_cols(reader.fieldnames, SHAPE_KEYS)
        if not c_name or not c_dur:
            degradations.append("kernel_details.csv 解析失败（缺少 Name/Duration 列）")
            return None
        unparsed_step = 0
        empty_step = 0
        suffix_norm = 0
        for r in reader:
            try:
                raw_name = (r.get(c_name) or "").strip()
                name = norm_name(raw_name)
                if name != raw_name:
                    suffix_norm += 1
                dur = float(r.get(c_dur) or 0)
            except (TypeError, ValueError):
                continue
            if not name or dur <= 0:
                continue
            step_raw = (r.get(c_step) or "").strip() if c_step else ""
            if not step_raw:
                empty_step += 1
            try:
                step = int(step_raw) if step_raw else 0
            except ValueError:
                step = 0
                unparsed_step += 1
            # Input/Output Shapes 都纳入，避免只改 output shape 的漂移被漏掉。
            shape = "|".join(
                (r.get(col) or "").strip() for col in c_shapes if (r.get(col) or "").strip()
            )
            rows.append(
                {
                    "step": step,
                    "name": name[:160],
                    "type": (r.get(c_type) or "").strip() if c_type else "",
                    "dur": dur,
                    "shape": shape,
                    "shape_known": bool(shape),
                }
            )
    total_rows = len(rows)
    if rows:
        if not c_step:
            degradations.append(
                "kernel_details.csv 无 Step Id 列，按整窗聚合（每步指标实为整窗总量）"
            )
        elif empty_step >= total_rows:
            degradations.append(
                "kernel_details.csv 的 Step Id 全为空，按整窗聚合（每步指标实为整窗总量）"
            )
        elif empty_step:
            notes.append(f"kernel_details.csv 有 {empty_step} 行 Step Id 为空，按 step0 归属")
        if unparsed_step:
            notes.append(f"kernel_details.csv 有 {unparsed_step} 行 Step Id 无法解析，按 step0 归属")
            if unparsed_step >= max(1, int(total_rows * 0.3)):
                degradations.append(
                    f"kernel_details.csv 有 {unparsed_step}/{total_rows} 行 Step Id 无法解析，"
                    "step 维度不可靠"
                )
    if suffix_norm:
        notes.append(f"kernel_details.csv 有 {suffix_norm} 个通信算子实例后缀已归一化")
    if not rows:
        degradations.append("kernel_details.csv 解析失败（无有效数据行）")
        return None
    if not c_shapes:
        degradations.append("kernel_details.csv 无 shape 列，shape 指标不可用")
    return rows, "kernel_details.csv"


def _resolve_strings(con):
    try:
        return {int(i): str(v) for i, v in con.execute("SELECT id, value FROM STRING_IDS")}
    except sqlite3.Error:
        return {}


def _db_spans(con):
    """STEP_TIME 给出绝对 step 边界；缺表/缺行时返回空。"""
    try:
        rows = con.execute("SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    return sorted((int(a), int(b), int(i)) for i, a, b in rows if b > a)


def _resolve(strings, value):
    """STRING_IDS 解码；负 id / 未命中返回空串，避免把 -1 当名字或 shape。"""
    try:
        key = int(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    if key < 0:
        return ""
    return strings.get(key, "") or ""


def _db_shape(strings, value):
    v = _resolve(strings, value).strip()
    if not v or v.upper() in ("N/A", "NONE", "NULL"):
        return ""
    return v


def _db_candidates(output_dir, notes):
    """列出可用 db，按 STEP_TIME 行数降序（仓内 analysis-nostack 的择优约定）。"""
    out = []
    for db in sorted(output_dir.glob("ascend_pytorch_profiler_*.db")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            tables = {r[0].lower() for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            spans = (_db_spans(con)
                     if {"task", "compute_task_info", "string_ids"} <= tables else None)
            con.close()
        except Exception as exc:
            notes.append(f"读取 {db.name} 失败: {exc}")
            continue
        if spans is not None:
            out.append((db, spans))
    out.sort(key=lambda x: (-len(x[1]), x[0].name))
    return out


def read_db(output_dir, notes, degradations, only_db=None):
    """读真实 torch_npu db：TASK(时间) JOIN COMPUTE_TASK_INFO(名字/shape) + STRING_IDS。

    TASK 本身没有 name/duration 列，算子名与 shape 在 COMPUTE_TASK_INFO，经 STRING_IDS 解码；
    该路径覆盖计算内核（AI_CORE/AI_VECTOR_CORE/MIX_*），通信/拷贝任务不在算子统计内。
    多 db 时按 STEP_TIME 行数择优，其余未纳入会写入 notes 与 degraded。
    """
    candidates = _db_candidates(output_dir, notes)
    if only_db is not None:
        candidates = [c for c in candidates if c[0].name == Path(only_db).name]
    for db, spans in candidates:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            strings = _resolve_strings(con)
            q = (
                "SELECT t.startNs, t.endNs, c.name, c.taskType, c.inputShapes, c.outputShapes "
                "FROM TASK t JOIN COMPUTE_TASK_INFO c ON t.globalTaskId = c.globalTaskId"
            )
            rows = []
            out_of_step = 0
            suffix_norm = 0
            for start_ns, end_ns, name_id, type_id, in_shape, out_shape in con.execute(q):
                try:
                    dur = (int(end_ns) - int(start_ns)) / 1000.0
                except (TypeError, ValueError):
                    continue
                raw_name = _resolve(strings, name_id)
                name = norm_name(raw_name)
                if name != raw_name:
                    suffix_norm += 1
                if not name or dur <= 0:
                    continue
                if spans:
                    step = None
                    for a, b, sid in spans:
                        if a <= int(start_ns) < b:
                            step = sid
                            break
                    if step is None:
                        out_of_step += 1
                        continue
                else:
                    step = 0
                shape = "|".join(
                    x for x in (_db_shape(strings, in_shape), _db_shape(strings, out_shape)) if x
                )
                rows.append({
                    "step": step,
                    "name": name[:160],
                    "type": _resolve(strings, type_id),
                    "dur": dur,
                    "shape": shape,
                    "shape_known": bool(shape),
                })
            con.close()
            if rows:
                if len(candidates) > 1:
                    msg = (f"发现 {len(candidates)} 个 ascend_pytorch_profiler_*.db，"
                           f"采用 {db.name}（STEP_TIME {len(spans)} 步），其余未纳入")
                    notes.append(msg)
                    degradations.append(msg)
                if (output_dir / "kernel_details.csv").exists():
                    degradations.append(
                        "kernel_details.csv 不可用，改用 db 的 TASK+COMPUTE_TASK_INFO（仅计算内核）"
                    )
                else:
                    degradations.append(
                        "kernel_details.csv 缺失，改用 db 的 TASK+COMPUTE_TASK_INFO（仅计算内核）"
                    )
                if out_of_step:
                    notes.append(f"db 有 {out_of_step} 个 task 不在 STEP_TIME 窗口内，已剔除")
                if suffix_norm:
                    notes.append(f"db 有 {suffix_norm} 个通信算子实例后缀已归一化")
                if not spans:
                    degradations.append("db 无 STEP_TIME，按整窗聚合")
                if not any(r["shape_known"] for r in rows):
                    degradations.append("db 路径无 shape 字段，shape 指标不可用")
                return rows, db.name
            con.close()
        except Exception as exc:
            notes.append(f"读取 {db.name} 失败: {exc}")
            continue
    return None


def _ns_to_us_guard(ts, dur):
    """trace 的 ts/dur 按 µs 解释（torch_npu/CANN chrome trace 规范）。"""
    return ts, dur


def _extract_complete_events(text):
    """从可能被截断的 trace 文本里提取完整的顶层事件对象（不依赖整文件 json）。"""
    start = text.find("[")
    if start < 0:
        return []
    depth = 0
    in_str = False
    esc = False
    obj_start = None
    events = []
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        events.append(json.loads(text[obj_start:i + 1]))
                    except Exception:
                        pass
                    obj_start = None
        elif ch == "]" and depth == 0:
            break
    return events


def _load_trace_events(trace_path, notes, degradations):
    """返回 trace 事件列表；兼容 list 与 {"traceEvents":[...]}，超大文件截断解析。"""
    size = trace_path.stat().st_size
    if size > MAX_TRACE_BYTES:
        degradations.append(
            f"trace_view.json 过大（{size / 1e6:.0f}MB > {MAX_TRACE_BYTES / 1e6:.0f}MB），"
            f"仅解析前 {TRACE_TRUNCATE_BYTES / 1e6:.0f}MB，指标可能偏小"
        )
        notes.append("trace_view.json 已截断解析：仅保留窗口内完整事件")
        with open(trace_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(TRACE_TRUNCATE_BYTES)
        return _extract_complete_events(text)
    data = json.loads(trace_path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        return data.get("traceEvents") or data.get("events") or []
    return data


def read_trace(trace_path, notes, degradations):
    if not trace_path or not Path(trace_path).exists():
        return None
    try:
        events = _load_trace_events(Path(trace_path), notes, degradations)
    except Exception as exc:
        notes.append(f"trace_view.json 解析失败: {exc}")
        return None
    if not isinstance(events, list):
        return None
    step_re = re.compile(r"ProfilerStep#(\d+)")
    spans, order = [], 0
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        try:
            ts = float(e.get("ts"))
            dur = float(e.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        name = str(e.get("name") or "")
        if name.startswith("ProfilerStep#"):
            m = step_re.match(name)
            spans.append((ts, ts + dur, int(m.group(1)) if m else order))
            order += 1
    spans.sort()
    starts = [s[0] for s in spans]
    if not spans:
        degradations.append("未找到 ProfilerStep#N，按整窗聚合")
    rows = []
    out_of_step = 0
    suffix_norm = 0
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        try:
            ts = float(e.get("ts"))
            dur = float(e.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        raw_name = str(e.get("name") or "").strip()
        name = norm_name(raw_name)
        if name != raw_name:
            suffix_norm += 1
        cat = str(e.get("cat") or "")
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        tt = str(args.get("Task Type") or args.get("task type") or "")
        # 只统计计算/通信内核；DMA SQE 属于拷贝维度（h2d-d2h），且
        # kernel_details.csv 不含它们，纳入会让 trace 与 CSV 口径差约 2 倍。
        # hcom 名称前缀优先于 Task Type：通信任务可能带 COMM_SQE 等非 AI Task Type。
        is_dev = name.startswith("hcom") or (
            (tt in AI_TASK_TYPES) if tt else (cat in AI_TASK_TYPES)
        )
        if not is_dev:
            continue
        ts_us, dur_us = _ns_to_us_guard(ts, dur)
        step = 0
        if spans:
            i = bisect_right(starts, ts_us) - 1
            if i >= 0 and ts_us < spans[i][1]:
                step = spans[i][2]
            else:
                # 不在任何 step 窗口内（warmup/teardown），归到 step0 会污染首步。
                out_of_step += 1
                continue
        rows.append({"step": step, "name": name[:160], "type": tt or cat,
                     "dur": dur_us, "shape": "", "shape_known": False})
    if out_of_step:
        notes.append(f"trace 有 {out_of_step} 个设备事件不在 ProfilerStep 窗口内，已剔除")
    if suffix_norm:
        notes.append(f"trace 有 {suffix_norm} 个通信算子实例后缀已归一化")
    if spans:
        seen = {r["step"] for r in rows}
        # spans 元素为 (start, end, step_id)，不能把 start 当 step id。
        for _a, _b, sid in spans:
            if sid not in seen:
                rows.append({"step": sid, "name": None, "type": "", "dur": 0.0,
                             "shape": "", "shape_known": False})
    if not rows:
        return None
    degradations.append("使用 trace_view.json 设备事件，无 shape 字段")
    return rows, "trace_view.json"


def _known_step_ids(output_dir, trace_path, source):
    """尽量收集完整 step 集合，供 aggregate 为没有算子命中的 step 补零。"""
    ids = set()
    if source == "kernel_details.csv":
        path = Path(output_dir) / "kernel_details.csv"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                col = _pick_col(reader.fieldnames or [], ("step id", "step"))
                if col:
                    for r in reader:
                        try:
                            ids.add(int((r.get(col) or "").strip()))
                        except (TypeError, ValueError):
                            continue
        except OSError:
            pass
    elif str(source).endswith(".db"):
        # 只从实际被读取的那个 db 收集 STEP_TIME，不能 glob 整目录混入其他 db 的 step。
        db = Path(output_dir) / str(source)
        if db.exists():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                rows = con.execute("SELECT id FROM STEP_TIME").fetchall()
                con.close()
                ids.update(int(r[0]) for r in rows if r[0] is not None)
            except Exception:
                pass
    elif source == "trace_view.json" and trace_path:
        try:
            events = _load_trace_events(Path(trace_path), [], [])
        except Exception:
            events = []
        for e in events:
            name = str(e.get("name") or "")
            if name.startswith("ProfilerStep#"):
                try:
                    ids.add(int(name.split("#", 1)[1].split()[0]))
                except (IndexError, ValueError):
                    continue
    return sorted(ids)


def load_capture(path, notes, degradations):
    output_dir, trace_path = locate(path)
    explicit_db = None
    pth = Path(path)
    if pth.is_file() and pth.name.startswith("ascend_pytorch_profiler_") and pth.suffix == ".db":
        explicit_db = pth
        notes.append(f"显式指定数据库文件 {pth.name}，只读取该文件")
    got = None if explicit_db else read_csv(output_dir, notes, degradations)
    if got:
        cap = {"rows": got[0], "source": got[1], "path": str(output_dir)}
    else:
        got = read_db(output_dir, notes, degradations, only_db=explicit_db)
        if got:
            cap = {"rows": got[0], "source": got[1],
                   "path": str(pth) if explicit_db else str(output_dir)}
        else:
            got = read_trace(trace_path, notes, degradations)
            if got:
                cap = {"rows": got[0], "source": got[1], "path": str(trace_path)}
            else:
                raise FileNotFoundError(
                    f"无法从 {path} 读取算子数据（无 kernel_details.csv / db TASK / trace 设备事件）")
    cap["known_steps"] = _known_step_ids(output_dir, trace_path, cap["source"])
    return cap


# ---------------------------------------------------------------------------
# 聚合与指标
# ---------------------------------------------------------------------------
def aggregate(cap):
    per_step = defaultdict(lambda: {"count": 0, "dur": 0.0})
    per_op = {}
    for r in cap["rows"]:
        st = per_step[r["step"]]
        if r["name"] is None:
            continue
        st["count"] += 1
        st["dur"] += r["dur"]
        # 归类前统一归一化实例后缀；读取层已归一化过，这里再做一次防御，
        # 避免直接构造 cap 或新数据源漏归一化时把同类多实例算成新增算子。
        name = norm_name(r["name"])
        slot = per_op.setdefault(
            name,
            {"count": 0, "dur": 0.0, "type": r["type"], "steps": defaultdict(float),
             "shapes": set(), "rows": 0, "shape_rows": 0},
        )
        slot["count"] += 1
        slot["dur"] += r["dur"]
        slot["steps"][r["step"]] += r["dur"]
        slot["rows"] += 1
        if r["shape_known"]:
            slot["shape_rows"] += 1
        if r["shape"]:
            slot["shapes"].add(re.sub(r"\s+", "", r["shape"]))
        if r["type"] and not slot["type"]:
            slot["type"] = r["type"]
    # 为已知但本侧没有算子命中的 step 补零，保证“每步”分母是完整 step 数，而不是
    # 只有算子记录的 step 数（否则每步调用数会被放大）。
    step_ids = sorted(set(per_step) | set(cap.get("known_steps") or []))
    for sid in step_ids:
        per_step.setdefault(sid, {"count": 0, "dur": 0.0})
    return {"per_step": dict(per_step), "per_op": per_op, "steps": step_ids}


def op_percentiles(per_op, p):
    vals = []
    for slot in per_op.values():
        vals.extend(v / 1000.0 for v in slot["steps"].values())
    return percentile(vals, p)


def sum_shape_drift(base_ops, cand_ops):
    """返回 (变化算子数, 基线不可解析行数, 候选不可解析行数, 明细)。"""
    changed = 0
    details = []
    for name in sorted(set(base_ops) & set(cand_ops)):
        b, c = base_ops[name], cand_ops[name]
        if b["shape_rows"] == 0 or c["shape_rows"] == 0:
            continue
        if b["shapes"] and c["shapes"] and b["shapes"] != c["shapes"]:
            changed += 1
            details.append((name, sorted(b["shapes"])[:3], sorted(c["shapes"])[:3]))
    b_unparsed = sum(s["rows"] - s["shape_rows"] for s in base_ops.values())
    c_unparsed = sum(s["rows"] - s["shape_rows"] for s in cand_ops.values())
    return changed, b_unparsed, c_unparsed, details


def metric(name, unit, base, cand, noise, evidence, info=False, pct=True):
    delta = base - cand
    if info:
        verdict = "info"
    elif delta > noise:
        verdict = "improved"
    elif delta < -noise:
        verdict = "regressed"
    else:
        verdict = "noise"
    return {
        "name": name,
        "unit": unit,
        "base": round4(base),
        "cand": round4(cand),
        "delta": round4(delta),
        "delta_pct": round4((delta / base * 100.0) if base else 0.0, 2) if pct else 0.0,
        "verdict": verdict,
        "noise_band": round4(noise),
        "evidence": evidence,
    }


def compare(base, cand, min_delta_ms, min_delta_count, rename_jaccard):
    notes = []
    ba, ca = aggregate(base), aggregate(cand)
    b_ops, c_ops = ba["per_op"], ca["per_op"]
    b_steps = max(1, len(ba["steps"]))
    c_steps = max(1, len(ca["steps"]))

    b_count = sum(s["count"] for s in b_ops.values())
    c_count = sum(s["count"] for s in c_ops.values())
    b_time = sum(s["dur"] for s in b_ops.values()) / 1000.0
    c_time = sum(s["dur"] for s in c_ops.values()) / 1000.0

    b_series = [ba["per_step"][s]["count"] for s in ba["steps"]]
    c_series = [ca["per_step"][s]["count"] for s in ca["steps"]]
    b_t_series = [ba["per_step"][s]["dur"] / 1000.0 for s in ba["steps"]]
    c_t_series = [ca["per_step"][s]["dur"] / 1000.0 for s in ca["steps"]]

    noise_c = max(min_delta_count, mad(b_series) + mad(c_series))
    noise_t = max(min_delta_ms, mad(b_t_series) + mad(c_t_series))

    b_p = [op_percentiles(b_ops, p) for p in (50, 95, 99)]
    c_p = [op_percentiles(c_ops, p) for p in (50, 95, 99)]
    b_pop = [v / 1000.0 for s in b_ops.values() for v in s["steps"].values()]
    c_pop = [v / 1000.0 for s in c_ops.values() for v in s["steps"].values()]
    noise_p = max(min_delta_ms, mad(b_pop) + mad(c_pop))

    new_ops = sorted(set(c_ops) - set(b_ops))
    gone_ops = sorted(set(b_ops) - set(c_ops))
    renames = []
    still_new, still_gone = [], []
    unmatched_gone = list(gone_ops)
    suffix_variants = []
    for n in new_ops:
        match = next((g for g in unmatched_gone if similar_name(n, g, rename_jaccard)), None)
        if match is None:
            still_new.append(n)
            continue
        # token 集完全相同 = 只差数字实例后缀（如 hcom_x_98_1 / hcom_x_293_1），
        # 视为同一算子的不同实例，不记为“改名”，也不计入新增/消失。
        if (tokens(match) == tokens(n)
                and str(n).lower().startswith("hcom")
                and str(match).lower().startswith("hcom")):
            suffix_variants.append((match, n))
            unmatched_gone.remove(match)
        else:
            renames.append((match, n))
            unmatched_gone.remove(match)
    # 兜底：实例后缀未被正则覆盖时，只要与 base 的某个 hcom 算子 token 集相同，
    # 仍视为同类实例，不计入新增。
    base_hcom_tokens = {frozenset(tokens(n)) for n in b_ops if str(n).lower().startswith("hcom")}
    remaining_new = []
    for n in still_new:
        if str(n).lower().startswith("hcom") and frozenset(tokens(n)) in base_hcom_tokens:
            suffix_variants.append((None, n))
        else:
            remaining_new.append(n)
    still_new = remaining_new
    still_gone = unmatched_gone

    shape_changed, shape_unparsed_b, shape_unparsed_c, shape_details = sum_shape_drift(b_ops, c_ops)

    ev = f"base:{base['source']} / cand:{cand['source']}"
    metrics = [
        metric("op_kind_count", "count", len(b_ops), len(c_ops), 0.0, ev, info=True),
        metric("op_call_count_per_step", "count", b_count / b_steps, c_count / c_steps,
               noise_c, ev),
        metric("op_time_ms_per_step", "ms", b_time / b_steps, c_time / c_steps, noise_t, ev),
        metric("op_p50_ms", "ms", b_p[0], c_p[0], noise_p, ev),
        metric("op_p95_ms", "ms", b_p[1], c_p[1], noise_p, ev),
        metric("op_p99_ms", "ms", b_p[2], c_p[2], noise_p, ev),
        metric("op_new_count", "count", 0, len(still_new), 0.0, ev, info=True, pct=False),
        metric("op_gone_count", "count", 0, len(still_gone), 0.0, ev, info=True, pct=False),
        metric("op_shape_changed_count", "count", 0, shape_changed, 0.0, ev, info=True, pct=False),
        metric("op_shape_unparsed_count", "count", shape_unparsed_b, shape_unparsed_c,
               0.0, ev, info=True, pct=False),
    ]

    if _source_kind(base["source"]) != _source_kind(cand["source"]):
        for m in metrics:
            m["verdict"] = "info"
        notes.append(
            f"两侧数据源类型不同（{base['source']} vs {cand['source']}），"
            "指标只展示数值、不做方向判定"
        )
    def _kinds(cap):
        out = set()
        for r in cap["rows"]:
            typ = (r.get("type") or "").upper()
            nm = str(r.get("name") or "").lower()
            if "COMM" in typ or nm.startswith("hcom"):
                out.add("comm")
            elif any(k in typ for k in ("DMA", "COPY")) or nm.startswith(("memcpy", "memset")):
                out.add("copy")
            else:
                out.add("compute")
        return out

    kinds_b, kinds_c = _kinds(base), _kinds(cand)
    if kinds_b != kinds_c:
        for m in metrics:
            if m["name"] in ("op_call_count_per_step", "op_time_ms_per_step",
                             "op_p50_ms", "op_p95_ms", "op_p99_ms"):
                m["verdict"] = "info"
        notes.append(
            f"两侧覆盖的任务类型不同（base={sorted(kinds_b)} / cand={sorted(kinds_c)}），"
            "受影响指标只展示数值、不做方向判定"
        )

    src = {m["name"]: m for m in metrics}
    set_part = f"种类 {len(b_ops)} → {len(c_ops)}，新增 {len(still_new)}、消失 {len(still_gone)}"
    parts = []
    t = src["op_time_ms_per_step"]
    if t["verdict"] != "noise":
        parts.append(f"每步算子耗时 {t['base']:.3f} → {t['cand']:.3f}ms（{t['delta']:+.3f}ms）")
    c = src["op_call_count_per_step"]
    if c["verdict"] != "noise":
        parts.append(f"每步调用 {c['base']:.2f} → {c['cand']:.2f}（{c['delta']:+.2f}）")
    if parts:
        summary = "；".join(parts + [set_part])
    elif still_new or still_gone:
        summary = f"算子维度耗时/次数在噪声内；{set_part}"
    elif shape_changed:
        summary = f"算子维度耗时/次数在噪声内，但检出 {shape_changed} 个算子 shape 变化"
    else:
        summary = "算子维度无实质变化（在噪声内）"

    if renames:
        notes.append("疑似改名：" + "、".join(f"{a} → {b}" for a, b in renames[:10]))
    if suffix_variants:
        notes.append(f"{len(suffix_variants)} 个算子仅数字实例后缀不同（视为同类，不计新增/消失）")
    if shape_details:
        notes.append(f"shape 变化示例 {len(shape_details)} 个（HTML 展示前 30 条）")
    notes.append(f"分位数样本量 base {len(b_pop)} / cand {len(c_pop)}，样本少时仅供参考")
    if min(len(ba["steps"]), len(ca["steps"])) < 3:
        notes.append("步数 < 3，MAD 不可靠，判定主要由 --min-delta-ms 决定")

    steps_n = min(len(ba["steps"]), len(ca["steps"]))
    per_step = []
    for i in range(steps_n):
        bs, cs = ba["steps"][i], ca["steps"][i]
        b_cnt = ba["per_step"][bs]["count"]
        c_cnt = ca["per_step"][cs]["count"]
        per_step.append({
            "step": bs,
            "cand_step": cs,
            "base": b_cnt,
            "cand": c_cnt,
            "delta": b_cnt - c_cnt,
            "base_time_ms": round4(ba["per_step"][bs]["dur"] / 1000.0),
            "cand_time_ms": round4(ca["per_step"][cs]["dur"] / 1000.0),
        })

    return {
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "base_ops": b_ops,
        "cand_ops": c_ops,
        "new_ops": still_new,
        "gone_ops": still_gone,
        "renames": renames,
        "shape_details": shape_details,
        "notes": notes,
    }


def _source_kind(src):
    """把数据源归一为类型，避免同类型不同文件名（如多个 db）被误判为跨源。"""
    if str(src).endswith(".db"):
        return "db"
    return src


def envelope(base, cand, result, base_notes, cand_notes, base_deg, cand_deg, base_src, cand_src):
    degraded = [f"base: {x}" for x in base_deg] + [f"cand: {x}" for x in cand_deg]
    if _source_kind(base_src) != _source_kind(cand_src):
        degraded.append(
            f"两侧数据源不同（{base_src} vs {cand_src}），算子集合/次数/耗时口径不可直接比较"
        )
    sidecars = sorted({base_src, cand_src})
    no_step_keys = ("整窗", "未找到 ProfilerStep", "无 STEP_TIME", "无有效 Step Id",
                    "step 维度不可靠")
    no_step = any(k in x for x in degraded for k in no_step_keys)
    if no_step:
        confidence = "low"
    elif degraded:
        confidence = "medium"
    else:
        confidence = "high"
    notes = []
    for tag, ns in (("base", base_notes), ("cand", cand_notes)):
        notes.extend(f"{tag}: {n}" for n in ns)
    notes.extend(result.get("notes") or [])
    notes.append("Δ = base − cand，正值表示候选更省")
    return {
        "skill": "kptools-ascend-profiling-compare-opstats",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": base["path"], "cand": cand["path"],
                   "steps": f"per_step={len(result['per_step'])}"},
        "data_quality": {"sidecars": sidecars, "degraded": degraded, "confidence": confidence},
        "metrics": result["metrics"],
        "per_step": result["per_step"],
        "summary": result["summary"],
        "notes": notes,
    }


def validate(env):
    errs = []
    for k in ("skill", "schema_version", "mode", "inputs", "data_quality",
              "metrics", "per_step", "summary", "notes"):
        if k not in env:
            errs.append(f"缺字段 {k}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 非法")
    dq = env.get("data_quality") or {}
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 非法")
    for i, m in enumerate(env.get("metrics") or []):
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}] 缺 evidence")
    if not env.get("metrics"):
        errs.append("metrics 为空")
    return errs


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _verdict_cls(v):
    return {"improved": "v-dev", "regressed": "v-host"}.get(v, "v-mid")


def render_html(env, result, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    rows = []
    for m in env["metrics"]:
        rows.append(
            f"<tr><td>{esc(m['name'])}</td><td>{m['base']}</td><td>{m['cand']}</td>"
            f"<td>{m['delta']:+.3f}</td><td>{m['delta_pct']:+.1f}%</td>"
            f"<td><span class='verdict {_verdict_cls(m['verdict'])}'>{esc(m['verdict'])}</span></td>"
            f"<td>±{m['noise_band']}</td><td class='note'>{esc(m['evidence'])}</td></tr>"
        )
    table = ("<div class='sect'><h3>指标对比</h3><table>"
             "<tr><th>指标</th><th>base</th><th>cand</th><th>Δ</th><th>变化</th>"
             "<th>判定</th><th>噪声带</th><th>证据</th></tr>"
             + "".join(rows) + "</table></div>")

    def name_list(items, empty):
        if not items:
            return f"<div class='note'>{empty}</div>"
        return "<div class='note'>" + "、".join(esc(x) for x in items[:40]) + \
               (f" 等 {len(items)} 个" if len(items) > 40 else "") + "</div>"

    sets = (
        "<div class='sect'><h3>新增算子（候选侧独有）</h3>" + name_list(result["new_ops"], "无") +
        "<h3>消失算子（基线侧独有）</h3>" + name_list(result["gone_ops"], "无") +
        "</div>"
    )

    rename_html = ""
    if result["renames"]:
        rename_html = ("<div class='sect'><h3>疑似改名（按名称相似度，不计入新增/消失）</h3><table>"
                       "<tr><th>base</th><th>cand</th></tr>"
                       + "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>"
                                 for a, b in result["renames"][:30])
                       + "</table></div>")

    shape_html = ""
    if result["shape_details"]:
        shape_html = ("<div class='sect'><h3>shape 变化</h3><table>"
                      "<tr><th>算子</th><th>base shape</th><th>cand shape</th></tr>"
                      + "".join(f"<tr><td>{esc(n)}</td><td>{esc(b)}</td><td>{esc(c)}</td></tr>"
                                for n, b, c in result["shape_details"][:30])
                      + "</table></div>")

    dq = env["data_quality"]
    caveats = ""
    if dq["degraded"] or env["notes"]:
        items = "".join(f"<li>{esc(x)}</li>" for x in (dq["degraded"] + env["notes"]))
        caveats = f"<div class='sect'><h3>降级说明与口径</h3><ul>{items}</ul></div>"

    hero = (
        "<div class='hero'>"
        f"<div class='htitle'>算子维度对比 · <span class='judge'>{esc(env['summary'])}</span></div>"
        f"<div class='hnums'>base {esc(env['inputs']['base'])}<span class='sep'>→</span>"
        f"cand {esc(env['inputs']['cand'])}</div>"
        f"<div class='kv'>契约置信度: {esc(dq['confidence'])} · 数据源: {esc(', '.join(dq['sidecars']))}</div>"
        "</div>"
    )

    return (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>算子统计对比</title><style>{css}</style></head><body>"
        f"<div class='page on'><h1>Profiling 算子统计对比</h1>{hero}{table}{sets}{rename_html}"
        f"{shape_html}{caveats}</div></body></html>"
    )


def render_error(msg, base_path, cand_path, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return (
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>算子对比失败</title>"
        f"<style>{css}</style></head><body><div class='page on'><h1>算子对比失败</h1>"
        f"<div class='hero'><div class='htitle'>无法完成对比</div>"
        f"<div class='hnums'>{esc(msg)}</div></div>"
        f"<div class='kv'>base: {esc(base_path)}</div><div class='kv'>cand: {esc(cand_path)}</div>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_SIDECARS = ("trace_view.json", "analysis.db", "kernel_details.csv", "step_trace_time.csv")


def _check_outputs(base, cand, out_html, out_json):
    outs = [Path(out_html).expanduser().resolve()]
    if out_json:
        outs.append(Path(out_json).expanduser().resolve())
        if outs[0] == outs[1]:
            raise ValueError("--out-html 与 --json 不能指向同一文件")
    inputs = set()
    for root in (base, cand):
        if not root:
            continue
        p = Path(root).expanduser().resolve()
        if p.is_file():
            inputs.add(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and (f.name in _SIDECARS or f.name.startswith("ascend_pytorch_profiler_")):
                    inputs.add(f.resolve())
    for out in outs:
        if out in inputs:
            raise ValueError(f"输出 {out} 指向输入采集文件，拒绝覆盖")


def main(argv=None):
    ap = argparse.ArgumentParser(description="两份 Ascend profiling 的算子统计对比")
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="opstats_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    ap.add_argument("--min-delta-count", type=float, default=0.0)
    ap.add_argument("--rename-jaccard", type=float, default=0.6)
    args = ap.parse_args(argv)

    # 读取输入前先拒绝危险的输出路径（与输入侧车同路径 / HTML 与 JSON 同名）。
    try:
        _check_outputs(args.base, args.cand, args.out_html, args.out_json)
    except ValueError as exc:
        print(f"输出路径不安全，拒绝覆盖：{exc}", file=sys.stderr)
        return 3

    here = Path(__file__).resolve().parent
    css_path = here.parent / "assets" / "report.css"

    try:
        b_notes, c_notes = [], []
        b_deg, c_deg = [], []
        base = load_capture(args.base, b_notes, b_deg)
        cand = load_capture(args.cand, c_notes, c_deg)
        result = compare(base, cand, args.min_delta_ms, args.min_delta_count,
                         args.rename_jaccard)
    except Exception as exc:
        Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_html).write_text(
            render_error(f"{type(exc).__name__}: {exc}", args.base, args.cand, css_path),
            encoding="utf-8")
        print(f"对比失败: {exc}", file=sys.stderr)
        print(f"错误报告 → {args.out_html}", file=sys.stderr)
        return 3

    env = envelope(base, cand, result, b_notes, c_notes, b_deg, c_deg,
                   base["source"], cand["source"])
    errs = validate(env)
    if errs:
        print("警告：JSON 不符合 A.1: " + "; ".join(errs), file=sys.stderr)

    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(
            json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {args.out_json}")

    Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_html).write_text(
        render_html(env, result, css_path), encoding="utf-8")
    print(f"HTML → {args.out_html}")

    print(f"结论: {env['summary']}")
    for m in env["metrics"][:4]:
        print(f"  {m['name']:<26} base {m['base']:>10} cand {m['cand']:>10} "
              f"Δ {m['delta']:+.4f} {m['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
