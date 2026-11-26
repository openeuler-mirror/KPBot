#!/usr/bin/env python3
"""Ascend profiling 设备气泡窗口检测与双份对比（kptools-ascend-profiling-compare-bubble）。

只回答「设备时间轴上空在哪个窗口、属于哪一类」，不输出占比结论（Free 占比归 v1）。

自包含实现：仅使用 python3 标准库，不 import 其它 skill 的脚本。
以下逻辑为复制并改写（未 import）：
- 采集定位 / 侧车读取 / 区间与统计工具：
  来源 kptools-ascend-profiling-compare/scripts/kpc_core.py
- trace_view.json 事件读取（大文件正则兜底）：
  来源 kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py

用法:
  bubble.py BASE [CAND] [--out-html report.html] [--json bubbles.json]
            [--min-gap-ms 0.01] [--adjacent-ms 1.0] [--max-bubble-ms X]
            [--min-delta-ms 0.05]
"""

import argparse
import bisect
import csv
import html
import json
import os
import re
import sqlite3
import sys
import traceback
from collections import defaultdict
from pathlib import Path

SCHEMA_VERSION = 1
SKILL_NAME = "kptools-ascend-profiling-compare-bubble"

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
# 拷贝引擎任务（真实 trace 中 H2D/D2H/D2D 以这些 Task Type 出现），同样占用设备。
COPY_TASK_TYPES = {"SDMA_SQE", "PCIE_DMA_SQE", "WRITE_VALUE_SQE", "MEM_WRITE_VALUE"}
DEVICE_TASK_TYPES = AI_TASK_TYPES | COPY_TASK_TYPES
COMM_CAT = "COMMUNICATION"
OA_CAT = "Overlap Analysis"

MIN_GAP_DEFAULT = 0.01
ADJACENT_DEFAULT = 1.0
MIN_DELTA_DEFAULT = 0.05

CLASS_LABELS = {
    "compute": "不可归因空窗",
    "comm_wait": "通信等待",
    "sync": "同步",
}

BUSY_CALIBER = {
    "trace_view.json:device_events": (
        "trace_view.json 设备事件 cat∈AI_TASK_TYPES|COPY_TASK_TYPES|COMMUNICATION|hcom*"
    ),
    "trace_view.json:overlap_analysis": (
        "trace_view.json Overlap Analysis Computing/Communication"
    ),
    "analysis.db:StepTraceTime(近似)": (
        "StepTraceTime Computing+Communication 时长近似"
    ),
}

VERDICT_LABELS = {
    "improved": "改善",
    "regressed": "恶化",
    "noise": "在噪声内",
    "info": "信息",
}

RE_STEP = re.compile(r"ProfilerStep#(\d+)")
RE_PH_X = re.compile(r'"ph"\s*:\s*"X"')


# ---------------------------------------------------------------------------
# 通用工具（复制自 kpc_core.py）
# ---------------------------------------------------------------------------
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


def _to_ns(v):
    """trace_view.json 的时间字段是 µs（可能是字符串），统一换算为 ns。"""
    try:
        return float(v) * 1000.0
    except (TypeError, ValueError):
        return 0.0


def _ms(ns):
    return float(ns) / 1e6


# ---------------------------------------------------------------------------
# 采集定位与读取（复制自 kpc_core.py）
# ---------------------------------------------------------------------------
_RANK_RE = re.compile(r"(?:^|[_\-])(?:rank|device)[_\-]?(\d+)$", re.IGNORECASE)


def _rank_of_dir(path):
    """从 profiler_info 或路径推断 rank；推断失败返回 None。"""
    path = Path(path)
    for base in (path, path.parent):
        for f in sorted(base.glob("profiler_info_*.json")):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rid = j.get("rank_id")
            if isinstance(rid, int):
                return rid
            if isinstance(rid, str) and rid.strip().isdigit():
                return int(rid)
    for part in reversed(path.parts):
        m = _RANK_RE.search(part)
        if m:
            return int(m.group(1))
    return None


def _select_capture(candidates, kind, rank):
    """多 rank 候选时确定性选择：优先指定 --rank，否则取 rank 最小的候选。

    路径仅作为同 rank/无 rank 时的稳定 tie-break，不依赖 rglob 的文件系统顺序。
    """
    items = []
    for out in candidates:
        r = _rank_of_dir(out)
        items.append((r, str(out), out))
    available = sorted({r for r, _s, _o in items if r is not None})
    if rank is not None:
        chosen = next((o for r, _s, o in items if r == rank), None)
        if chosen is None:
            raise ValueError(
                f"输入中未找到 rank={rank} 的{kind}；可用 rank: {available or '未知'}"
            )
    else:
        known = [it for it in items if it[0] is not None]
        if known:
            chosen = min(known, key=lambda it: (it[0], it[1]))[2]
        else:
            chosen = min(items, key=lambda it: it[1])[2]
    selected = _rank_of_dir(chosen)
    return chosen, {
        "selected_rank": selected,
        "available_ranks": available,
        "capture_count": len(items),
        "dropped_count": max(0, len(items) - 1),
        "kind": kind,
    }


def locate_capture(path, rank=None):
    """返回 (output_dir, profiler_root, trace_path, selection_info)。"""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        if p.name == "trace_view.json":
            out = p.parent
        else:
            out = p.parent
            p = p.parent / "trace_view.json"
        r = _rank_of_dir(out)
        if rank is not None and r is not None and r != rank:
            raise ValueError(f"输入 rank={r}，与指定 --rank {rank} 不一致")
        return out, out.parent, p, {
            "selected_rank": r, "available_ranks": [r] if r is not None else [],
            "capture_count": 1, "dropped_count": 0, "kind": "trace_view",
        }
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")

    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        r = _rank_of_dir(direct)
        return direct, p, direct / "trace_view.json", {
            "selected_rank": r, "available_ranks": [r] if r is not None else [],
            "capture_count": 1, "dropped_count": 0, "kind": "direct",
        }
    if (p / "analysis.db").exists() or (p / "step_trace_time.csv").exists() or (
        p / "trace_view.json"
    ).exists():
        r = _rank_of_dir(p)
        return p, p.parent, p / "trace_view.json", {
            "selected_rank": r, "available_ranks": [r] if r is not None else [],
            "capture_count": 1, "dropped_count": 0, "kind": "direct",
        }

    outs = sorted(p.rglob("ASCEND_PROFILER_OUTPUT"), key=lambda x: str(x))
    if outs:
        chosen, info = _select_capture(outs, "ASCEND_PROFILER_OUTPUT", rank)
        return chosen, chosen.parent, chosen / "trace_view.json", info
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: str(x))
    if traces:
        dirs = [t.parent for t in traces]
        chosen, info = _select_capture(dirs, "trace_view.json", rank)
        return chosen, chosen.parent, chosen / "trace_view.json", info
    return p, p.parent, p / "trace_view.json", {
        "selected_rank": None, "available_ranks": [], "capture_count": 0,
        "dropped_count": 0, "kind": "empty",
    }


def _event_from_obj(e):
    if not isinstance(e, dict) or e.get("ph") != "X":
        return None
    name = e.get("name")
    ts = e.get("ts")
    if name is None or ts is None:
        return None
    args = e.get("args") if isinstance(e.get("args"), dict) else {}
    # Task Type 只从当前事件的 args 读取；raw_decode 保证不会跨事件串染。
    cat = e.get("cat") or args.get("Task Type")
    return (
        _to_ns(ts),
        _to_ns(e.get("dur") or 0),
        str(name),
        cat,
        e.get("pid"),
        e.get("tid"),
    )


def _parse_events_fallback(text):
    """大 trace 兜底：逐对象 raw_decode，兼容任意键序与带引号的 dur。

    不再用跨事件的正则搜索 Task Type，避免 host 事件被下一事件的 Task Type 染色。
    返回 (events, meta)，meta 用于发现解析丢失并写入 degradations。
    """
    decoder = json.JSONDecoder()
    events = []
    idx = 0
    n = len(text)
    while idx < n:
        i = text.find("{", idx)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            idx = i + 1
            continue
        idx = end
        if isinstance(obj, dict) and isinstance(obj.get("traceEvents") or obj.get("events"), list):
            inner = obj.get("traceEvents") or obj.get("events")
            for e in inner:
                ev = _event_from_obj(e)
                if ev is not None:
                    events.append(ev)
            continue
        ev = _event_from_obj(obj)
        if ev is not None:
            events.append(ev)
    markers = len(RE_PH_X.findall(text))
    return events, {"fallback": True, "markers": markers, "parsed": len(events)}


def _read_events(trace_path, force_fallback=False):
    """读取 trace 事件，返回 (events, meta)。"""
    path = Path(trace_path)
    if not path.exists():
        return [], {}
    try:
        size = path.stat().st_size
    except OSError:
        return [], {}

    if size <= 512 * 1024 * 1024 and not force_fallback:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                data = data.get("traceEvents") or data.get("events") or []
            events = [ev for ev in (_event_from_obj(e) for e in data) if ev is not None]
            return events, {"fallback": False, "parsed": len(events)}
        except Exception:
            pass

    text = path.read_text(encoding="utf-8", errors="replace")
    return _parse_events_fallback(text)


def _read_step_trace(output_dir):
    """读取 analysis.db:StepTraceTime 或 step_trace_time.csv（µs）。"""
    rows = []
    source = None
    adb = output_dir / "analysis.db"
    if adb.exists():
        try:
            con = sqlite3.connect(f"file:{adb}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT step,computing,communication,free,stage FROM StepTraceTime ORDER BY step"
            ).fetchall()
            con.close()
            source = "analysis.db:StepTraceTime"
        except Exception:
            rows = []
    if not rows:
        csv_path = output_dir / "step_trace_time.csv"
        if csv_path.exists():
            with open(csv_path, encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append(
                            (
                                int((r.get("Step") or "").strip()),
                                float(r.get("Computing") or 0),
                                float(r.get("Communication") or 0),
                                float(r.get("Free") or 0),
                                float(r.get("Stage") or 0),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            if rows:
                source = "step_trace_time.csv"

    agg = {}
    for sid, comp, comm, free, stage in rows:
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        slot = agg.setdefault(
            sid, {"comp": 0.0, "comm": 0.0, "free": 0.0, "stage": 0.0}
        )
        slot["comp"] += float(comp)
        slot["comm"] += float(comm)
        slot["free"] += float(free)
        slot["stage"] += float(stage)
    return agg, source


def _read_step_bounds(output_dir):
    """读取 ascend_pytorch_profiler_*.db:STEP_TIME 绝对边界（ns）。"""
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


def _read_meta(output_dir, root):
    for f in sorted(output_dir.glob("profiler_info_*.json")) + sorted(
        root.glob("profiler_info_*.json")
    ):
        try:
            with open(f, encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception:
            continue
        return {
            "profiler_info": f.name,
            "version": j.get("torch_npu_version"),
            "cann_version": j.get("cann_version"),
            "rank": j.get("rank_id"),
        }
    return {"profiler_info": None, "version": None, "cann_version": None, "rank": None}


def _profiler_steps(events):
    out = {}
    for ts, dur, name, _cat, _pid, _tid in events:
        if dur <= 0:
            continue
        m = RE_STEP.search(name)
        if not m:
            continue
        sid = int(m.group(1))
        out.setdefault(sid, (sid, ts, ts + dur))
    return sorted(out.values())


# ---------------------------------------------------------------------------
# 采集加载
# ---------------------------------------------------------------------------
def load_capture(path, label=None, rank=None):
    notes = []
    degradations = []
    sidecars = []
    output_dir, root, trace_path, selection = locate_capture(path, rank)

    step_trace, step_trace_source = _read_step_trace(output_dir)
    if step_trace_source:
        sidecars.append(step_trace_source.split(":")[0])
    kind = "native" if step_trace_source else "trace_view"

    bounds, bounds_name = _read_step_bounds(output_dir)
    if bounds_name:
        sidecars.append(bounds_name)

    events, event_meta = _read_events(trace_path)
    if Path(trace_path).exists():
        sidecars.append(Path(trace_path).name)
    if event_meta.get("fallback"):
        markers = event_meta.get("markers") or 0
        parsed = event_meta.get("parsed") or 0
        if markers and parsed < markers:
            degradations.append(
                f"大 trace 兜底解析事件数 {parsed}/{markers}，存在事件丢失，"
                "设备忙碌区间与气泡可能失真"
            )

    if bounds:
        steps = [
            {"id": sid, "start_ns": start, "end_ns": end}
            for sid, (start, end) in sorted(bounds.items())
        ]
        step_source = "ascend_pytorch_profiler_*.db:STEP_TIME"
    else:
        steps = [
            {"id": sid, "start_ns": start, "end_ns": end}
            for sid, start, end in _profiler_steps(events)
        ]
        step_source = "trace_view.json:ProfilerStep" if steps else None
        if step_trace and not steps:
            degradations.append(
                "有 StepTraceTime 但缺 STEP_TIME 绝对边界，无法按 step 归属气泡"
            )

    device_items = []
    for ts, dur, name, cat, _pid, _tid in events:
        if dur <= 0:
            continue
        if (
            cat in DEVICE_TASK_TYPES
            or cat == COMM_CAT
            or (name and name.lower().startswith("hcom"))
        ):
            device_items.append((ts, ts + dur, name, cat))

    oa_items = []
    if not device_items:
        for ts, dur, name, cat, _pid, _tid in events:
            if dur <= 0:
                continue
            if name not in ("Computing", "Communication"):
                continue
            # 真实 trace 的 Overlap Analysis 事件常表现为 cat=None（仅靠进程名标识），
            # 因此同时接受 cat=Overlap Analysis 与 cat=None 的 Computing/Communication。
            if cat == OA_CAT or cat is None:
                oa_items.append((ts, ts + dur, name, OA_CAT))

    busy_source = None
    if device_items:
        busy_items = device_items
        busy_source = "trace_view.json:device_events"
    elif oa_items:
        busy_items = oa_items
        busy_source = "trace_view.json:overlap_analysis"
        degradations.append("无 device task 事件，忙碌区间改用 Overlap Analysis 事件")
    elif step_trace and steps:
        busy_items = []
        for s in steps:
            m = step_trace.get(s["id"])
            if not m:
                continue
            busy_ns = (m["comp"] + m["comm"]) * 1000.0
            if busy_ns <= 0:
                continue
            end = min(s["end_ns"], s["start_ns"] + busy_ns)
            busy_items.append((s["start_ns"], end, "~StepTraceTime", "APPROX"))
        busy_source = "analysis.db:StepTraceTime(近似)"
        degradations.append(
            "无 device 事件，忙碌区间按 StepTraceTime 的 Computing+Communication 时长近似"
        )
        notes.append("近似窗口无法做通信邻接判定（busy_comm 为空，通信等待不可达）；同步分类仍按窗口内 host synchronize 判定")
    else:
        busy_items = []
        notes.append("未发现 device 事件且无可用的 StepTraceTime 近似数据")
        degradations.append("无 device 事件，无法构建设备忙碌区间，气泡列表为空")

    busy = [(s, e) for s, e, _n, _c in busy_items]
    if busy_source and busy_source.endswith("(近似)"):
        busy_comm = []
    else:
        busy_comm = [
            (s, e)
            for s, e, name, cat in busy_items
            if cat == COMM_CAT
            or (name and "hcom" in name.lower())
            or (cat == OA_CAT and name == "Communication")
        ]

    sync_events = [
        (ts, ts + dur, name)
        for ts, dur, name, _cat, _pid, _tid in events
        if dur > 0 and "synchronize" in name.lower()
    ]

    meta = _read_meta(output_dir, root)
    if meta["profiler_info"]:
        sidecars.append(meta["profiler_info"])

    dropped = selection.get("dropped_count") or 0
    if dropped > 0:
        avail = selection.get("available_ranks") or []
        notes.append(
            f"检测到 {selection['capture_count']} 个 rank/capture，"
            f"仅分析 rank={selection.get('selected_rank')}（其余 {dropped} 个未纳入）；"
            f"可用 rank={avail}"
        )
        degradations.append(
            f"输入含 {selection['capture_count']} 个 rank，仅分析 rank="
            f"{selection.get('selected_rank')}，属单 rank 抽样结论"
        )

    window = None
    if busy:
        window = (min(s for s, _e in busy), max(e for _s, e in busy))

    return {
        "label": label or Path(path).name,
        "path": str(output_dir),
        "kind": kind,
        "busy_source": busy_source,
        "step_source": step_source,
        "steps": steps,
        "busy": busy,
        "busy_items": busy_items,
        "busy_comm": busy_comm,
        "sync_events": sync_events,
        "window": window,
        "sidecars": list(dict.fromkeys(sidecars)),
        "notes": notes,
        "degradations": degradations,
        "selected_rank": selection.get("selected_rank"),
        "available_ranks": selection.get("available_ranks") or [],
        **{k: meta[k] for k in ("version", "cann_version", "rank")},
    }


# ---------------------------------------------------------------------------
# 气泡检测与分类
# ---------------------------------------------------------------------------
def _prepare_index(cap):
    """把 busy/sync/comm/steps 预排序，供每个气泡 O(logN) 查询邻域。"""
    busy_items = cap.get("busy_items") or []
    busy_starts = sorted((s, name or "") for s, _e, name, _cat in busy_items)
    busy_ends = sorted((e, name or "") for _s, e, name, _cat in busy_items)

    comm = cap.get("busy_comm") or []
    comm_starts = sorted(s for s, _e in comm)
    comm_ends = sorted(e for _s, e in comm)

    sync = cap.get("sync_events") or []
    sync_sorted = sorted((s, e) for s, e, _name in sync)
    sync_starts = [s for s, _e in sync_sorted]
    sync_max_end = []
    cur = float("-inf")
    for _s, e in sync_sorted:
        cur = max(cur, e)
        sync_max_end.append(cur)

    step_spans = sorted((s["start_ns"], s["end_ns"], s["id"]) for s in cap.get("steps") or [])
    return {
        "busy_starts": busy_starts,
        "busy_ends": busy_ends,
        "comm_starts": comm_starts,
        "comm_ends": comm_ends,
        "sync_starts": sync_starts,
        "sync_max_end": sync_max_end,
        "step_spans": step_spans,
        "step_starts": [s for s, _e, _i in step_spans],
    }


def _nearest_name(sorted_pairs, target, tol):
    """在 (time, name) 有序数组里找时间最接近 target 且距其 ≤ tol 的名字。"""
    i = bisect.bisect_left(sorted_pairs, (target, ""))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(sorted_pairs):
            key, name = sorted_pairs[j]
            d = abs(key - target)
            if d <= tol:
                cand = (d, name)
                if best is None or cand < best:
                    best = cand
    return best[1] if best else None


def _step_of(ts, index):
    spans = index["step_spans"]
    i = bisect.bisect_right(index["step_starts"], ts) - 1
    if i >= 0:
        _s, e, sid = spans[i]
        if ts < e:
            return sid
    return None


def _has_sync_overlap(start, end, index):
    # 以 start 排序 + 前缀最大 end：存在 s < end 且 e > start 的区间。
    i = bisect.bisect_left(index["sync_starts"], end)
    return i > 0 and index["sync_max_end"][i - 1] > start


def _comm_adjacent(start, end, index, adjacent_ns):
    i = bisect.bisect_left(index["comm_ends"], start - adjacent_ns)
    if i < len(index["comm_ends"]) and index["comm_ends"][i] <= start + adjacent_ns:
        return True
    j = bisect.bisect_left(index["comm_starts"], end - adjacent_ns)
    if j < len(index["comm_starts"]) and index["comm_starts"][j] <= end + adjacent_ns:
        return True
    return False


def _adjacent_names(start, end, index, adjacent_ns):
    return (
        _nearest_name(index["busy_ends"], start, adjacent_ns),
        _nearest_name(index["busy_starts"], end, adjacent_ns),
    )


def detect_bubbles(cap, min_gap_ms=MIN_GAP_DEFAULT, adjacent_ms=ADJACENT_DEFAULT,
                   max_bubble_ms=None):
    """返回 (bubbles, info)；bubbles 按起点升序。"""
    busy = cap["busy"]
    window = cap["window"]
    info = {"filtered_short": 0, "filtered_long": 0}
    if not busy or not window:
        return [], info

    w0, w1 = window
    gaps = [
        (s, e)
        for s, e in _complement(busy, w0, w1)
        if e > s
    ]
    min_ns = max(0.0, min_gap_ms) * 1e6
    max_ns = max_bubble_ms * 1e6 if max_bubble_ms is not None else None
    adjacent_ns = max(0.0, adjacent_ms) * 1e6

    index = _prepare_index(cap)
    bubbles = []
    for start, end in gaps:
        dur_ns = end - start
        if dur_ns < min_ns:
            info["filtered_short"] += 1
            continue
        if max_ns is not None and dur_ns > max_ns:
            info["filtered_long"] += 1
            continue
        if _has_sync_overlap(start, end, index):
            cls = "sync"
        elif _comm_adjacent(start, end, index, adjacent_ns):
            cls = "comm_wait"
        else:
            cls = "compute"
        bubbles.append(
            {
                "start_ns": start,
                "end_ns": end,
                "dur_ms": _round(_ms(dur_ns)),
                "cls": cls,
                "step": _step_of(start, index),
            }
        )
    return bubbles, info


# ---------------------------------------------------------------------------
# 对比与 A.1 信封
# ---------------------------------------------------------------------------
def _series_for(cap, bubbles, kind):
    """返回 (per-step 序列, scale)。kind: count/total/max/<class>。"""
    steps = cap["steps"]
    if steps:
        grouped = defaultdict(list)
        for b in bubbles:
            grouped[b["step"]].append(b)
        vals = []
        for s in steps:
            bs = grouped.get(s["id"], [])
            if kind == "count":
                vals.append(float(len(bs)))
            elif kind == "max":
                vals.append(max((b["dur_ms"] for b in bs), default=0.0))
            elif kind == "total":
                vals.append(sum(b["dur_ms"] for b in bs))
            else:
                vals.append(sum(b["dur_ms"] for b in bs if b["cls"] == kind))
        return vals, len(steps)
    if kind == "count":
        return [float(len(bubbles))], 1
    if kind == "max":
        return [max((b["dur_ms"] for b in bubbles), default=0.0)], 1
    if kind == "total":
        return [b["dur_ms"] for b in bubbles] or [0.0], 1
    return [b["dur_ms"] for b in bubbles if b["cls"] == kind] or [0.0], 1


def _aggregate(cap, bubbles, kind):
    if kind == "count":
        return float(len(bubbles))
    if kind == "max":
        return max((b["dur_ms"] for b in bubbles), default=0.0)
    if kind == "total":
        return sum(b["dur_ms"] for b in bubbles)
    return sum(b["dur_ms"] for b in bubbles if b["cls"] == kind)


def _bullets(bubbles, kind):
    """无 step 边界时的序列，必须与 _series_for 的无 step 语义一致：
    count 取气泡总数、max 取最长气泡、total 取总时长，而不是逐气泡/恒 1 列表，
    否则 pair 模式下 count 恒为 1、max/total 退化为逐气泡中位数。"""
    if kind == "count":
        vals = [float(len(bubbles))]
    elif kind == "max":
        vals = [max((b["dur_ms"] for b in bubbles), default=0.0)]
    elif kind == "total":
        vals = [sum(b["dur_ms"] for b in bubbles)]
    else:
        vals = [sum(b["dur_ms"] for b in bubbles if b["cls"] == kind)]
    return vals or [0.0], 1


def _sign_consistency(paired):
    pos = sum(1 for d in paired if d > 1e-9)
    neg = sum(1 for d in paired if d < -1e-9)
    nonzero = pos + neg
    return round(max(pos, neg) / nonzero, 3) if nonzero else 1.0


def _build_metric(name, unit, kind, cap_b, bubbles_b, cap_c, bubbles_c,
                  single, min_delta_ms, min_delta_count, evidence):
    by_step = bool(cap_b["steps"] and cap_c and cap_c["steps"])
    series_b, _scale_b = _series_for(cap_b, bubbles_b, kind) if by_step else _bullets(bubbles_b, kind)
    if single:
        # 单份采集保留整窗聚合值（用户看“总时长/数量”更直观），不做判定。
        return {
            "name": name,
            "unit": unit,
            "base": _round(_aggregate(cap_b, bubbles_b, kind)),
            "cand": None,
            "delta": None,
            "delta_pct": None,
            "verdict": "info",
            "noise_band": 0.0,
            "evidence": evidence,
        }

    series_c, _scale_c = _series_for(cap_c, bubbles_c, kind) if by_step else _bullets(bubbles_c, kind)
    paired = None
    if cap_b["steps"] and cap_c["steps"]:
        # 有 step 时按位置配对，Δ 与噪声都落在 per-step 量纲，verdict 不随 step 数翻转。
        n = min(len(series_b), len(series_c))
        sb, sc = series_b[:n], series_c[:n]
        paired = [sb[i] - sc[i] for i in range(n)]
        base_val = _round(_median(sb))
        cand_val = _round(_median(sc))
        paired_delta = _round(_median(paired))
        raw_noise = _mad(paired)
    else:
        # 无 step 时逐气泡序列的中位数之差；噪声用两侧离散度，二者同为“单气泡”量纲。
        base_val = _round(_median(series_b))
        cand_val = _round(_median(series_c))
        paired_delta = None
        raw_noise = _mad(series_b) + _mad(series_c)

    delta = base_val - cand_val
    if unit == "ms":
        noise = _round(max(min_delta_ms, raw_noise))
    else:
        # 计数类绝对下限：默认 1，避免稳定负载下 1 个气泡差异被判成改善/恶化。
        noise = _round(max(min_delta_count, raw_noise))
        evidence = evidence + f"; count noise floor={min_delta_count}"
    judge = paired_delta if paired_delta is not None else delta
    if judge > noise:
        verdict = "improved"
    elif judge < -noise:
        verdict = "regressed"
    else:
        verdict = "noise"
    return {
        "name": name,
        "unit": unit,
        "base": base_val,
        "cand": cand_val,
        "delta": _round(delta),
        "delta_pct": _round(delta / base_val * 100.0, 2) if base_val else 0.0,
        "verdict": verdict,
        "noise_band": noise,
        "paired_delta": paired_delta,
        "sign_consistency": _sign_consistency(paired) if paired is not None else None,
        "evidence": evidence,
    }


def _per_step_pair(cap_b, bubbles_b, cap_c, bubbles_c):
    if not cap_b["steps"] or not cap_c["steps"]:
        return []
    sums_b = defaultdict(float)
    for b in bubbles_b:
        sums_b[b["step"]] += b["dur_ms"]
    sums_c = defaultdict(float)
    for c in bubbles_c:
        sums_c[c["step"]] += c["dur_ms"]
    out = []
    for sb, sc in zip(cap_b["steps"], cap_c["steps"]):
        b = sums_b.get(sb["id"], 0.0)
        c = sums_c.get(sc["id"], 0.0)
        out.append(
            {
                "step": sb["id"],
                "cand_step": sc["id"],
                "base": _round(b),
                "cand": _round(c),
                "delta": _round(b - c),
            }
        )
    return out


def _per_step_single(cap_b, bubbles_b):
    if not cap_b["steps"]:
        return []
    sums = defaultdict(float)
    for b in bubbles_b:
        sums[b["step"]] += b["dur_ms"]
    return [
        {
            "step": s["id"],
            "base": _round(sums.get(s["id"], 0.0)),
            "cand": None,
            "delta": None,
        }
        for s in cap_b["steps"]
    ]


def _confidence(caps, degradations):
    if not all(cap["steps"] for cap in caps):
        return "low"
    if all(cap["kind"] == "native" for cap in caps) and not degradations:
        return "high"
    return "medium"


def _bubble_evidence(cap, min_gap_ms):
    source = cap["busy_source"] or "none"
    caliber = BUSY_CALIBER.get(source, source)
    return f"busy_source: {source}; {caliber}; min_gap={min_gap_ms}ms"


def compare_captures(cap_b, cap_c=None, min_gap_ms=MIN_GAP_DEFAULT,
                     adjacent_ms=ADJACENT_DEFAULT, max_bubble_ms=None,
                     min_delta_ms=MIN_DELTA_DEFAULT, min_delta_count=1.0):
    single = cap_c is None
    bubbles_b, info_b = detect_bubbles(cap_b, min_gap_ms, adjacent_ms, max_bubble_ms)
    if single:
        bubbles_c, info_c = [], {"filtered_short": 0, "filtered_long": 0}
    else:
        bubbles_c, info_c = detect_bubbles(cap_c, min_gap_ms, adjacent_ms, max_bubble_ms)

    degradations = []
    notes = []
    for side, cap, info in (("base", cap_b, info_b), ("cand", cap_c, info_c)):
        if cap is None:
            continue
        for msg in cap["degradations"]:
            degradations.append(f"{side}: {msg}")
        for msg in cap["notes"]:
            notes.append(f"{side}: {msg}")
        if info["filtered_short"]:
            notes.append(
                f"{side}: {info['filtered_short']} 个空闲窗口短于 --min-gap-ms={min_gap_ms}ms，已忽略"
            )
        if info["filtered_long"]:
            notes.append(
                f"{side}: {info['filtered_long']} 个空闲窗口长于 --max-bubble-ms="
                f"{max_bubble_ms}ms，已过滤"
            )
    notes.append(
        "第四类「下发不足」unsupported：依赖 host 下发数据（T1-2），本批未实现；本报告只含不可归因空窗/通信等待/同步三类"
    )
    notes.append(
        "compute 类口径为「不可归因空窗」：窗口内无设备任务且不邻接通信/同步，仅表示无法归因，不证明是计算气泡或存在因果"
    )
    notes.append(
        "本 skill 不输出气泡占比结论；每步 Free 占比见 kptools-ascend-profiling-compare（v1）"
    )
    notes.append(
        "JSON 只含元数据/指标/per_step，不含原始 busy 与逐气泡区间；"
        "HTML 时间轴在超过 2000 段时按 800 色带聚合，Top 10 明细仍保留"
    )
    notes.append(
        "噪声判定: 有 step 时按位置配对，noise = max(--min-delta-ms, MAD(逐对 Δ))，"
        "verdict 由 paired_delta 判定；无 step 时用逐气泡中位数之差与两侧离散度"
    )
    notes.append(
        "指标口径: pair 模式下 metrics 的 base/cand 为 per-step（无 step 时为逐气泡）中位数；"
        "summary 中的整窗总时长/数量为聚合值，两者不可混用"
    )

    evidence_b = _bubble_evidence(cap_b, min_gap_ms)
    evidence_c = (
        _bubble_evidence(cap_c, min_gap_ms) if not single else evidence_b
    )
    evidence = (
        f"{evidence_b} / {evidence_c}"
        if not single
        else evidence_b
    )

    metrics = [
        _build_metric("bubble_count", "count", "count", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + "; bubbles.json:每个空闲窗口计 1"),
        _build_metric("bubble_total_ms", "ms", "total", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + "; bubbles.json:sum(dur_ms)"),
        _build_metric("bubble_max_ms", "ms", "max", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + "; bubbles.json:max(dur_ms)"),
        _build_metric("bubble_compute_ms", "ms", "compute", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + "; bubbles.json:class=compute(不可归因空窗)"),
        _build_metric("bubble_comm_wait_ms", "ms", "comm_wait", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + f"; bubbles.json:class=comm_wait(±{adjacent_ms}ms 邻接通信 kernel)"),
        _build_metric("bubble_sync_ms", "ms", "sync", cap_b, bubbles_b,
                      cap_c, bubbles_c, single, min_delta_ms, min_delta_count,
                      evidence + "; bubbles.json:class=sync(窗口内 host synchronize)"),
    ]
    metric_map = {m["name"]: m for m in metrics}
    unobservable = (not single) and (
        not cap_b.get("window") or not cap_c.get("window")
        or not cap_b.get("busy_source") or not cap_c.get("busy_source")
    )
    if unobservable:
        for m in metrics:
            m["verdict"] = "info"
        notes.append("至少一侧缺少设备窗口/忙碌来源，气泡不可观测；仅展示观测值，不做方向判定")

    if single:
        per_step = _per_step_single(cap_b, bubbles_b)
        summary = (
            f"单份采集：检出 {len(bubbles_b)} 个气泡，总时长 "
            f"{metric_map['bubble_total_ms']['base']:.3f}ms"
            f"（不可归因空窗 {metric_map['bubble_compute_ms']['base']:.3f}ms / "
            f"通信等待 {metric_map['bubble_comm_wait_ms']['base']:.3f}ms / "
            f"同步 {metric_map['bubble_sync_ms']['base']:.3f}ms）"
        )
    else:
        if unobservable:
            summary = "设备窗口不可观测（至少一侧无设备事件/侧车），气泡比较不作为性能结论"
        per_step = _per_step_pair(cap_b, bubbles_b, cap_c, bubbles_c)
        if len(cap_b["steps"]) != len(cap_c["steps"]):
            notes.append(
                f"base/cand step 数不一致（{len(cap_b['steps'])} vs "
                f"{len(cap_c['steps'])}），per_step 按位置配对，多出的 step 已截断"
            )
        total = metric_map["bubble_total_ms"]
        if not unobservable and not bubbles_b and not bubbles_c:
            summary = "两份采集均未检出气泡（设备窗口内无空闲）"
        else:
            verdict = VERDICT_LABELS.get(total["verdict"], total["verdict"])
            tot_b = _aggregate(cap_b, bubbles_b, "total")
            tot_c = _aggregate(cap_c, bubbles_c, "total")
            max_b = _aggregate(cap_b, bubbles_b, "max")
            max_c = _aggregate(cap_c, bubbles_c, "max")
            summary = (
                f"每步气泡总时长中位数 {total['base']:.3f}ms → {total['cand']:.3f}ms"
                f"（Δ {total['delta']:+.3f}ms，{verdict}）；"
                f"整窗总时长 {tot_b:.3f}ms → {tot_c:.3f}ms；"
                f"数量 {len(bubbles_b)}→{len(bubbles_c)}，"
                f"最长 {max_b:.3f}ms→{max_c:.3f}ms"
            )

    confidence = _confidence(
        [cap_b] if single else [cap_b, cap_c], degradations
    )

    result = {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "single" if single else "pair",
        "inputs": {
            "base": cap_b["path"],
            "cand": None if single else cap_c["path"],
            "steps": (
                f"base:{cap_b['step_source'] or 'none'}; cand:"
                f"{(cap_c['step_source'] if not single else 'n/a') or 'none'}"
            ),
        },
        "min_gap_ms": min_gap_ms,
        "adjacent_ms": adjacent_ms,
        "max_bubble_ms": max_bubble_ms,
        "base": _meta_of(cap_b),
        "cand": _meta_of(cap_c),
        "bubbles": {"base": bubbles_b, "cand": bubbles_c},
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "notes": notes,
        "degradations": degradations,
        "data_quality": {
            "sidecars": list(
                dict.fromkeys(
                    cap_b["sidecars"] + ([] if single else cap_c["sidecars"])
                )
            ),
            "degraded": degradations,
            "confidence": confidence,
        },
    }
    return result


def validate(env):
    """A.1 信封自检（规则复制自 kptools-ascend-profiling-compare/scripts/envelope.py）。"""
    errs = []
    for key in (
        "skill",
        "schema_version",
        "mode",
        "inputs",
        "data_quality",
        "metrics",
        "per_step",
        "summary",
        "notes",
    ):
        if key not in env:
            errs.append(f"缺字段 {key}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 必须是 pair | single")

    dq = env.get("data_quality") or {}
    for key in ("sidecars", "degraded", "confidence"):
        if key not in dq:
            errs.append(f"data_quality 缺字段 {key}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("data_quality.confidence 必须是 high | medium | low")
    if not isinstance(dq.get("sidecars"), list):
        errs.append("data_quality.sidecars 必须是数组")
    if not isinstance(dq.get("degraded"), list):
        errs.append("data_quality.degraded 必须是数组")

    metrics = env.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errs.append("metrics 必须是非空数组")
        metrics = metrics if isinstance(metrics, list) else []
    for i, m in enumerate(metrics):
        for key in (
            "name",
            "unit",
            "base",
            "cand",
            "delta",
            "delta_pct",
            "verdict",
            "noise_band",
            "evidence",
        ):
            if key not in m:
                errs.append(f"metrics[{i}] 缺字段 {key}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法: {m.get('verdict')}")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 必须是数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 必须是数组")
    return errs


# ---------------------------------------------------------------------------
# HTML 报告
# ---------------------------------------------------------------------------
CSS_PATH = Path(__file__).resolve().parent.parent / "assets" / "report.css"

TIMELINE_CSS = """
.tl{position:relative;height:30px;background:#FAF8F5;border:1px solid #E8E4DC;
    border-radius:6px;overflow:hidden;margin:8px 0 2px}
.tl-seg{position:absolute;top:0;height:100%;opacity:.92;
        border-right:1px solid rgba(255,255,255,.55)}
.tl-busy{background:#DFDBD2}
.tl-compute{background:#4A78A0}
.tl-comm_wait{background:#B08968}
.tl-sync{background:#B5838D}
.tl-axis{display:flex;justify-content:space-between;font-size:11.5px;color:#8B9299;margin:0 0 10px}
.unsupported{color:#B08968;font-weight:600}
"""


def _esc(s):
    return html.escape(str(s), quote=True)


def _css():
    try:
        return CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _num(x, n=3):
    try:
        return f"{float(x):.{n}f}"
    except (TypeError, ValueError):
        return "—"


def _verdict_cls(verdict):
    return {
        "improved": "v-dev",
        "regressed": "v-host",
        "noise": "v-mid",
        "info": "v-mid",
    }.get(verdict, "v-mid")


def _table(headers, rows):
    out = ["<table><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


_META_KEYS = (
    "label", "path", "kind", "busy_source", "step_source", "window",
    "sidecars", "notes", "degradations", "version", "cann_version", "rank",
    "selected_rank", "available_ranks",
)


def _meta_of(cap):
    if not cap:
        return None
    return {k: cap.get(k) for k in _META_KEYS if k in cap}


def to_json_envelope(result):
    """JSON 只保留元数据 + 指标 + per_step；原始区间和逐气泡明细不落盘。

    HTML 仍使用内存中的完整 cap / bubbles，因此必须在 render_html 之前调用
    （或保留原 result 供 HTML 使用）。
    """
    out = dict(result)
    out["base"] = _meta_of(result.get("base"))
    out["cand"] = _meta_of(result.get("cand"))
    out.pop("bubbles", None)
    out["notes"] = list(result.get("notes") or []) + [
        "JSON 不含原始 busy/逐气泡区间；时间轴与 Top10 明细在 HTML 中，"
        "事件过多时时间轴按色带聚合显示"
    ]
    return out


TIMELINE_MAX_SEGMENTS = 2000
TIMELINE_BINS = 800


def _timeline_binned(w0, w1, busy, bubbles):
    span = max(1.0, float(w1 - w0))
    votes = [dict() for _ in range(TIMELINE_BINS)]

    def add(start, end, state):
        a = max(0, int((start - w0) / span * TIMELINE_BINS))
        b = min(TIMELINE_BINS - 1, int((end - w0) / span * TIMELINE_BINS))
        for i in range(a, b + 1):
            lo = w0 + span * i / TIMELINE_BINS
            hi = w0 + span * (i + 1) / TIMELINE_BINS
            overlap = min(end, hi) - max(start, lo)
            if overlap > 0:
                votes[i][state] = votes[i].get(state, 0.0) + overlap

    for start, end in busy:
        add(start, end, "busy")
    for b in bubbles:
        add(b["start_ns"], b["end_ns"], b["cls"])

    states = []
    bin_span = span / TIMELINE_BINS
    for v in votes:
        if not v:
            states.append("empty")
            continue
        bubble_votes = sum(w for k, w in v.items() if k not in ("busy", "empty"))
        busy_votes = v.get("busy", 0.0)
        # 聚合色带里保留可见的空窗：气泡占比达到 25% 即按气泡类展示。
        if bubble_votes > 0 and bubble_votes >= busy_votes * 0.25:
            candidates = {k: w for k, w in v.items() if k not in ("busy", "empty")}
            states.append(max(candidates, key=candidates.get))
        elif busy_votes > 0:
            states.append("busy")
        else:
            states.append("empty")
    merged = []
    for i, state in enumerate(states):
        start = w0 + span * i / TIMELINE_BINS
        end = w0 + span * (i + 1) / TIMELINE_BINS
        if merged and merged[-1][2] == state:
            merged[-1] = (merged[-1][0], end, state)
        else:
            merged.append((start, end, state))
    parts = ['<div class="tl">']
    for start, end, state in merged:
        left = max(0.0, (start - w0) / span * 100.0)
        width = max(0.12, (end - start) / span * 100.0)
        cls = "tl-busy" if state in ("busy", "empty") else f"tl-{state}"
        parts.append(
            f'<div class="tl-seg {cls}" style="left:{left:.3f}%;width:{width:.3f}%" '
            f'title="聚合色带 {state} [{int(start)}, {int(end)}]"></div>'
        )
    parts.append("</div>")
    return "".join(parts) + (
        '<div class="note">事件过多，时间轴已按 '
        f"{TIMELINE_BINS} 个色带聚合显示；逐窗口明细见 Top 10 与 HTML 内存数据。</div>"
    )


def _timeline(cap, bubbles):
    window = cap.get("window")
    if not window or "busy" not in cap:
        return (
            '<div class="note">当前 result 不含原始区间；时间轴需在生成报告的进程内'
            "（render_html(result, base_cap, cand_cap)）渲染。</div>"
        )
    if not window:
        return (
            '<div class="note">无可用设备时间窗口（未发现 device 事件），'
            "无法绘制时间轴。</div>"
        )
    w0, w1 = window
    span = max(1.0, float(w1 - w0))
    busy = _union(cap["busy"])
    if len(busy) + len(bubbles) > TIMELINE_MAX_SEGMENTS:
        return _timeline_binned(w0, w1, busy, bubbles)
    parts = ['<div class="tl">']
    for start, end in busy:
        left = max(0.0, (start - w0) / span * 100.0)
        width = max(0.12, (end - start) / span * 100.0)
        parts.append(
            f'<div class="tl-seg tl-busy" style="left:{left:.3f}%;width:{width:.3f}%" '
            f'title="忙碌 {_num(_ms(end - start))}ms [{int(start)}, {int(end)}]"></div>'
        )
    for b in bubbles:
        left = max(0.0, (b["start_ns"] - w0) / span * 100.0)
        width = max(0.18, (b["end_ns"] - b["start_ns"]) / span * 100.0)
        parts.append(
            f'<div class="tl-seg tl-{b["cls"]}" style="left:{left:.3f}%;width:{width:.3f}%" '
            f'title="{_esc(CLASS_LABELS[b["cls"]])} {_num(b["dur_ms"])}ms '
            f'[{int(b["start_ns"])}, {int(b["end_ns"])}]"></div>'
        )
    parts.append("</div>")
    axis = (
        '<div class="tl-axis">'
        f"<span>{int(w0)} ns</span>"
        f"<span>窗口 {_num(_ms(w1 - w0))}ms</span>"
        f"<span>{int(w1)} ns</span></div>"
    )
    legend = (
        '<div class="barleg">'
        '<i style="background:#DFDBD2"></i>设备忙碌'
        '<i style="background:#4A78A0"></i>不可归因空窗'
        '<i style="background:#B08968"></i>通信等待'
        '<i style="background:#B5838D"></i>同步'
        '<i style="background:#FAF8F5;border:1px solid #E8E4DC"></i>窗口未覆盖'
        "</div>"
    )
    return "".join(parts) + axis + legend


def render_html(result, base_cap=None, cand_cap=None):
    css = _css()
    pair = result["mode"] == "pair"
    cap_b = base_cap if base_cap is not None else result["base"]
    cap_c = cand_cap if cand_cap is not None else result["cand"]
    metric_map = {m["name"]: m for m in result["metrics"]}

    mode_label = "双份对比（base vs cand）" if pair else "单份分析（不做 Δ）"
    banner = ""
    if not pair:
        banner = (
            '<div class="note" style="border-left:4px solid #B08968;padding-left:10px">'
            "单份输入：只输出气泡窗口清单与分类，不做前后 Δ 判定。</div>"
        )
    elif not result["bubbles"]["base"] and not result["bubbles"]["cand"]:
        banner = (
            '<div class="note" style="border-left:4px solid #A6BE9E;padding-left:10px">'
            "两份采集均未检出气泡。</div>"
        )

    hero = (
        '<div class="hero"><div class="htitle">设备气泡窗口 · '
        f'<span class="judge">{_esc(mode_label)}</span></div>'
        f'<div class="hnums">{_esc(result["summary"])}</div>'
        f'<div class="barleg">窗口阈值 --min-gap-ms={result["min_gap_ms"]}ms · '
        f'邻接容差 --adjacent-ms={result["adjacent_ms"]}ms · '
        f'最长过滤 --max-bubble-ms={result["max_bubble_ms"] if result["max_bubble_ms"] is not None else "不过滤"}'
        "</div></div>"
    )

    src = (
        f'<div class="kv">base: {_esc(cap_b["label"])} · {_esc(cap_b["kind"])} · '
        f'忙碌来源={_esc(cap_b["busy_source"] or "none")} · '
        f'step={_esc(cap_b["step_source"] or "none")} · '
        f'torch_npu={_esc(cap_b["version"] or "?")} · rank={_esc(cap_b["rank"] if cap_b["rank"] is not None else "?")}</div>'
    )
    if pair:
        src += (
            f'<div class="kv">cand: {_esc(cap_c["label"])} · {_esc(cap_c["kind"])} · '
            f'忙碌来源={_esc(cap_c["busy_source"] or "none")} · '
            f'step={_esc(cap_c["step_source"] or "none")} · '
            f'torch_npu={_esc(cap_c["version"] or "?")} · rank={_esc(cap_c["rank"] if cap_c["rank"] is not None else "?")}</div>'
        )
    quality = (
        f'<div class="kv">契约置信度: {_esc(result["data_quality"]["confidence"])} · '
        f'侧车: {_esc(", ".join(result["data_quality"]["sidecars"]) or "无（仅 trace_view.json）")}</div>'
    )

    metric_rows = []
    for m in result["metrics"]:
        metric_rows.append(
            [
                _esc(m["name"]),
                _esc(m["unit"]),
                _num(m["base"], 0 if m["unit"] == "count" else 3)
                if m["base"] is not None else "—",
                _num(m["cand"], 0 if m["unit"] == "count" else 3)
                if m["cand"] is not None else "—",
                f'{m["delta"]:+.3f}' if m["delta"] is not None else "—",
                _num(m["noise_band"]),
                f'<span class="{_verdict_cls(m["verdict"])}">'
                f'{_esc(VERDICT_LABELS.get(m["verdict"], m["verdict"]))}</span>',
            ]
        )
    metric_table = _table(
        ["指标", "单位", "base", "cand", "Δ(base−cand)", "噪声带", "判定"], metric_rows
    )

    cat_rows = []
    for cls in ("compute", "comm_wait", "sync"):
        name = f"bubble_{cls}_ms"
        m = metric_map.get(name)
        if not m:
            continue
        cat_rows.append(
            [
                _esc(CLASS_LABELS[cls]),
                _num(m["base"]),
                _num(m["cand"]) if m["cand"] is not None else "—",
                f'{m["delta"]:+.3f}' if m["delta"] is not None else "—",
                f'<span class="{_verdict_cls(m["verdict"])}">'
                f'{_esc(VERDICT_LABELS.get(m["verdict"], m["verdict"]))}</span>',
            ]
        )
    cat_rows.append(
        [
            '<span class="unsupported">下发不足（unsupported）</span>',
            "—",
            "—",
            "—",
            "依赖 host 下发数据（T1-2），本批不做",
        ]
    )
    cat_table = _table(["分类", "base (ms)", "cand (ms)", "Δ", "判定/说明"], cat_rows)

    rows = []
    for side, cap, bubbles in (
        ("base", cap_b, result["bubbles"]["base"]),
        ("cand", cap_c, result["bubbles"]["cand"]),
    ):
        if cap is None:
            continue
        for b in bubbles:
            rows.append((side, b))
    rows.sort(key=lambda x: (-x[1]["dur_ms"], x[1]["start_ns"]))
    # 相邻 kernel 名只对 Top 10 计算，避免为全部气泡做邻域查询。
    index_cache = {}
    adjacent_ns = max(0.0, float(result.get("adjacent_ms") or 0.0)) * 1e6
    top_rows = []
    for side, b in rows[:10]:
        cap = cap_b if side == "base" else cap_c
        left = right = None
        if cap is not None and "busy" in cap:
            if side not in index_cache:
                index_cache[side] = _prepare_index(cap)
            left, right = _adjacent_names(
                b["start_ns"], b["end_ns"], index_cache[side], adjacent_ns
            )
        top_rows.append(
            [
                _esc(side),
                _esc(CLASS_LABELS[b["cls"]]),
                str(int(b["start_ns"])),
                str(int(b["end_ns"])),
                _num(b["dur_ms"]),
                "—" if b["step"] is None else f'step{b["step"]}',
                _esc((left or "—") + " / " + (right or "—")),
            ]
        )
    if not top_rows:
        top_rows = [["—", "—", "—", "—", "—", "—", "未检出气泡"]]
    top_table = _table(
        ["采集", "分类", "起点 (ns)", "终点 (ns)", "时长 (ms)", "归属 step", "相邻 kernel(左/右)"],
        top_rows,
    )

    timeline_b = _timeline(cap_b, result["bubbles"]["base"])
    timeline_c = ""
    if pair:
        timeline_c = (
            f'<h3>气泡时间轴 · cand（{_esc(cap_c["label"])}）</h3>'
            + _timeline(cap_c, result["bubbles"]["cand"])
        )

    step_rows = []
    for p in result["per_step"][:200]:
        step_rows.append(
            [
                f'step{p["step"]}' + (
                    f' / step{p["cand_step"]}' if "cand_step" in p else ""
                ),
                _num(p["base"]) if p["base"] is not None else "—",
                _num(p["cand"]) if p["cand"] is not None else "—",
                f'{p["delta"]:+.3f}' if p["delta"] is not None else "—",
            ]
        )
    if step_rows:
        more = ""
        if len(result["per_step"]) > 200:
            more = (
                f'<div class="note">仅展示前 200 步；其余 '
                f'{len(result["per_step"]) - 200} 步见 JSON per_step。</div>'
            )
        step_html = (
            '<div class="sect"><h3>每步气泡总时长</h3>'
            + _table(["step", "base (ms)", "cand (ms)", "Δ"], step_rows)
            + more
            + "</div>"
        )
    else:
        step_html = (
            '<div class="sect"><h3>每步气泡总时长</h3>'
            '<div class="note">无可靠 step 边界（缺 STEP_TIME / ProfilerStep），'
            "per_step 为空，置信度记 low。</div></div>"
        )

    if result["degradations"]:
        deg_html = (
            '<div class="sect"><h3>降级说明</h3><ul>'
            + "".join(f"<li>{_esc(x)}</li>" for x in result["degradations"])
            + "</ul></div>"
        )
    else:
        deg_html = (
            '<div class="sect"><h3>降级说明</h3>'
            '<div class="note">无降级：两侧均使用原生侧车与 trace 设备事件。</div></div>'
        )

    notes_html = (
        '<div class="sect"><h3>口径与限制</h3><ul>'
        + "".join(f"<li>{_esc(x)}</li>" for x in result["notes"])
        + "</ul></div>"
    )

    pair_open = '<div class="sect">' if pair else ""
    pair_close = "</div>" if pair else ""
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>Ascend Profiling 设备气泡报告</title>"
        f"<style>{css}{TIMELINE_CSS}</style></head><body>"
        '<nav><button class="on">设备气泡</button></nav>'
        '<div class="page on" id="home"><h1>Profiling 对比 · 设备气泡窗口</h1>'
        f"{banner}{hero}{src}{quality}"
        '<div class="sect"><h3>指标总览</h3>' + metric_table + "</div>"
        f'<div class="sect"><h3>气泡时间轴 · base（{_esc(cap_b["label"])}）</h3>'
        f"{timeline_b}</div>"
        f"{pair_open}{timeline_c}{pair_close}"
        '<div class="sect"><h3>分类时长</h3>' + cat_table + "</div>"
        '<div class="sect"><h3>Top 10 最长气泡窗口</h3>' + top_table
        + '<div class="note">起止为 trace_view.json 的绝对时间戳（ns），可直接回 trace 定位。</div></div>'
        f"{step_html}{deg_html}{notes_html}"
        "</div></body></html>"
    )


def render_error(base_path, cand_path, error, detail=""):
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        "<title>设备气泡检测失败</title>"
        f"<style>{_css()}{TIMELINE_CSS}</style></head><body>"
        '<div class="page on" id="home"><h1>设备气泡检测失败</h1>'
        '<div class="hero" style="border-left-color:#B08968">'
        '<div class="htitle">无法完成气泡检测</div>'
        f'<div class="hnums">{_esc(error)}</div></div>'
        f'<div class="kv">base: {_esc(base_path)}</div>'
        f'<div class="kv">cand: {_esc(cand_path)}</div>'
        '<div class="note">排查建议：确认输入目录含 ASCEND_PROFILER_OUTPUT 或 '
        "trace_view.json；纯 host trace 会输出空气泡列表并降级。</div>"
        f'<div class="sect"><h3>详细堆栈</h3><pre>{_esc(detail)}</pre></div>'
        "</div></body></html>"
    )


def error_envelope(base_path, cand_path, reason, detail=""):
    # 错误壳仅用于人工排查，metrics 为空，不符合 A.1（A.1 要求 metrics 非空）；
    # 调用方须以退出码 3 判定失败，不得按 A.1 消费本 JSON。
    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "pair" if cand_path else "single",
        "inputs": {"base": str(base_path), "cand": str(cand_path) if cand_path else None,
                   "steps": "n/a"},
        "data_quality": {"sidecars": [], "degraded": [reason], "confidence": "low"},
        "metrics": [],
        "per_step": [],
        "summary": "气泡检测失败",
        "notes": [reason, detail] if detail else [reason],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def format_text(result):
    lines = ["=" * 72, result["summary"], "-" * 72]
    for m in result["metrics"]:
        cand = "—" if m["cand"] is None else f'{m["cand"]:.3f}'
        delta = "—" if m["delta"] is None else f'{m["delta"]:+.3f}'
        lines.append(
            f'{m["name"]:<22} base {m["base"]:>10.3f}  cand {cand:>10}  '
            f'Δ {delta:>9}  {VERDICT_LABELS.get(m["verdict"], m["verdict"])}'
        )
    for note in result["notes"]:
        lines.append(f"  - {note}")
    return "\n".join(lines)


def _input_files(*roots):
    out = set()
    for root in roots:
        if not root:
            continue
        p = Path(root).expanduser()
        if p.is_file():
            out.add(p.resolve())
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and (f.name in ("trace_view.json", "analysis.db", "kernel_details.csv")
                                    or f.name.startswith("ascend_pytorch_profiler_")):
                    out.add(f.resolve())
    return out


def _check_outputs(base, cand, out_html, out_json):
    inputs = _input_files(base, cand)
    if Path(out_html).expanduser().resolve() == Path(out_json).expanduser().resolve():
        raise ValueError("--out-html 与 --json 不能指向同一文件")
    for flag, path in (("--out-html", out_html), ("--json", out_json)):
        p = Path(path).expanduser()
        if p.is_symlink():
            raise ValueError(f"{flag} {p} 是符号链接，拒绝写入")
        if p.resolve() in inputs:
            raise ValueError(f"{flag} {p} 指向输入采集文件，拒绝覆盖")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="检测 Ascend profiling 设备气泡窗口并做双份对比"
    )
    ap.add_argument("base", help="基线采集（目录/trace_view.json/ASCEND_PROFILER_OUTPUT）")
    ap.add_argument("cand", nargs="?", default=None, help="候选采集（缺省为单份模式）")
    ap.add_argument("--out-html", default=os.path.join(os.getcwd(), "profiling_bubble_report.html"))
    ap.add_argument("--json", dest="out_json", default=os.path.join(os.getcwd(), "bubbles.json"))
    ap.add_argument("--min-gap-ms", type=float, default=MIN_GAP_DEFAULT)
    ap.add_argument("--adjacent-ms", type=float, default=ADJACENT_DEFAULT)
    ap.add_argument("--max-bubble-ms", type=float, default=None)
    ap.add_argument("--min-delta-ms", type=float, default=MIN_DELTA_DEFAULT)
    ap.add_argument("--min-delta-count", type=float, default=1.0,
                    help="计数类指标的绝对噪声下限（默认 1 个气泡）")
    ap.add_argument("--label-base", default=None)
    ap.add_argument("--label-cand", default=None)
    ap.add_argument("--rank", type=int, default=None,
                    help="多 rank 采集时指定 rank；缺省取 rank 最小的候选")
    args = ap.parse_args(argv)

    try:
        _check_outputs(args.base, args.cand, args.out_html, args.out_json)
    except ValueError as exc:
        print(f"输出路径不安全，拒绝写入：{exc}", file=sys.stderr)
        return 3

    try:
        base = load_capture(args.base, args.label_base, args.rank)
        cand = load_capture(args.cand, args.label_cand, args.rank) if args.cand else None
        result = compare_captures(
            base,
            cand,
            min_gap_ms=args.min_gap_ms,
            adjacent_ms=args.adjacent_ms,
            max_bubble_ms=args.max_bubble_ms,
            min_delta_ms=args.min_delta_ms,
            min_delta_count=args.min_delta_count,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        detail = traceback.format_exc()
        _write(args.out_html, render_error(args.base, args.cand, reason, detail))
        _write(
            args.out_json,
            json.dumps(
                error_envelope(args.base, args.cand, reason, detail),
                ensure_ascii=False,
                indent=2,
            ),
        )
        print(f"气泡检测失败，已输出错误报告: {args.out_html}", file=sys.stderr)
        return 3

    print(format_text(result))
    errs = validate(result)
    if errs:
        print("警告：JSON 信封不符合 A.1：" + "; ".join(errs), file=sys.stderr)
    _write(
        args.out_json,
        json.dumps(to_json_envelope(result), ensure_ascii=False, indent=2),
    )
    _write(args.out_html, render_html(result, base, cand))
    print(f"JSON → {args.out_json}")
    print(f"HTML → {args.out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
