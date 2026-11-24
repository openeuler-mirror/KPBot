#!/usr/bin/env python3
"""Host 下发（enqueue/launch）耗时统计。

回答“host 把算子下发到 NPU 花了多少时间、下发到设备启动隔了多久、下发与执行重叠多少”。

数据来源（优先级递降）：
  1. ascend_pytorch_profiler_*.db：PYTORCH_API(op/queue) + CONNECTION_IDS + TASK +
     COMPUTE_TASK_INFO，可做 op → device task 的精确配对；
  2. trace_view.json：enqueue/dequeue 事件按 correlation_id 精确配对；
     仅在 trace 时，dequeue → 设备任务按时间窗启发式配对，配对率写入报告。

用法：
  python3 scripts/host_dispatch.py <capture|trace_view.json> \
    --json result.json --out-html report.html [--skip-warmup K]
"""

import argparse
import bisect
import glob
import html
import importlib.util
import json
import math
import sqlite3
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
SKILLS_DIR = SKILL_DIR.parent
CSS_PATH = SKILL_DIR / "assets" / "report.css"

OA_NAMES = {"Computing", "Communication", "Communication(Not Overlapped)", "Free"}
HOST_CATS = {"cpu_op", "enqueue", "dequeue"}
AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def _ns(value):
    return int(round(float(value) * 1000.0))


def _ms(ns):
    return ns / 1e6


def pct(values, q):
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return 0.0
    idx = min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))
    return round(xs[idx], 3)


def union(intervals):
    out = []
    for a, b in sorted((int(s), int(e)) for s, e in intervals if e > s):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def cover(intervals, a, b):
    return sum(max(0, min(e, b) - max(s, a)) for s, e in union(intervals))


def intersect_cover(a_ivs, b_ivs, a, b):
    """两个区间集合的交集总长度（双指针，避免 O(n*m)）。"""
    A = [iv for iv in union(a_ivs) if iv[1] > a and iv[0] < b]
    B = [iv for iv in union(b_ivs) if iv[1] > a and iv[0] < b]
    total, i, j = 0, 0, 0
    while i < len(A) and j < len(B):
        lo = max(A[i][0], B[j][0], a)
        hi = min(A[i][1], B[j][1], b)
        if hi > lo:
            total += hi - lo
        if A[i][1] < B[j][1]:
            i += 1
        else:
            j += 1
    return total


def hist_bins(values, bins=12):
    xs = [float(v) for v in values if v is not None]
    if not xs:
        return None
    lo, hi = min(xs), max(xs)
    if hi <= lo:
        hi = lo + 1.0
    counts = [0] * bins
    for v in xs:
        idx = min(bins - 1, int((v - lo) / (hi - lo) * bins))
        counts[idx] += 1
    return {"unit": "ms", "lo": round(lo, 3), "hi": round(hi, 3), "counts": counts}


def locate_capture(path):
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return p.parent, p
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    out = p / "ASCEND_PROFILER_OUTPUT"
    if out.is_dir():
        trace = out / "trace_view.json"
        return out, trace if trace.exists() else None
    trace = p / "trace_view.json"
    if trace.exists() or (p / "analysis.db").exists():
        return p, trace if trace.exists() else None
    # 直接指向 ASCEND_PROFILER_OUTPUT（只含 ascend_pytorch_profiler_*.db）也要能进入分析。
    if _find_dbs(p):
        return p, None
    traces = sorted(p.rglob("trace_view.json"))
    if traces:
        return traces[0].parent, traces[0]
    raise FileNotFoundError(f"未找到 trace_view.json / profiling 侧车: {p}")


def load_trace(trace_path):
    if not trace_path or not Path(trace_path).exists():
        return [], None, 0
    with open(trace_path, encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("traceEvents") or data.get("events") or []
    events = []
    ui_zero = None
    for e in data:
        if not isinstance(e, dict) or e.get("ph") != "X" or e.get("ts") is None:
            continue
        if ui_zero is None:
            ui_zero = _ns(e["ts"])
        events.append(
            (
                _ns(e["ts"]),
                _ns(e.get("dur") or 0),
                str(e.get("name") or ""),
                e.get("cat"),
                e.get("pid"),
                e.get("tid"),
                e.get("args") or {},
            )
        )
    events.sort(key=lambda x: x[0])
    host_count = {}
    for _, _, _, cat, pid, _, _ in events:
        if cat in HOST_CATS:
            host_count[pid] = host_count.get(pid, 0) + 1
    host_pid = max(host_count, key=host_count.get) if host_count else (events[0][4] if events else None)
    return events, host_pid, ui_zero or 0


def is_device_task(ev, host_pid):
    _, dur, name, cat, pid, _, args = ev
    if dur <= 0 or pid == host_pid or name in OA_NAMES:
        return False
    if "Synchronize" in name or "ProfilerStep#" in name:
        return False
    return ("task id" in args) or (cat in AI_TASK_TYPES) or (name in ("Memcpy", "Memset"))


# ---------------------------------------------------------------------------
# 侧车
# ---------------------------------------------------------------------------
def _find_dbs(out_dir):
    return sorted(glob.glob(str(out_dir / "ascend_pytorch_profiler_*.db")))


def _load_conn_map(con):
    try:
        return {int(i): int(c) for i, c in con.execute("SELECT id, connectionId FROM CONNECTION_IDS")}
    except sqlite3.Error:
        return {}


def _load_string_ids(con):
    try:
        return {int(i): str(v) for i, v in con.execute("SELECT id, value FROM STRING_IDS")}
    except sqlite3.Error:
        return {}


def _load_tasks(con):
    """connectionId -> (min_start_ns, globalTaskId)。"""
    out = {}
    try:
        rows = con.execute(
            "SELECT connectionId, startNs, globalTaskId FROM TASK WHERE connectionId >= 0"
        )
    except sqlite3.Error:
        return out
    for cid, start, gid in rows:
        cid = int(cid)
        start = int(start)
        prev = out.get(cid)
        if prev is None or start < prev[0]:
            out[cid] = (start, int(gid))
    return out


def _load_task_intervals(con):
    """TASK 的设备忙碌区间（startNs,endNs）；无 endNs 列时返回空（标记不可用）。"""
    try:
        rows = con.execute(
            "SELECT startNs, endNs FROM TASK WHERE connectionId >= 0"
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for start, end in rows:
        try:
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            continue
        if end > start:
            out.append((start, end))
    return out


def _has_metric_data(con):
    for sql in (
        "SELECT 1 FROM PYTORCH_API LIMIT 1",
        "SELECT 1 FROM CANN_API LIMIT 1",
        "SELECT 1 FROM TASK LIMIT 1",
    ):
        try:
            if con.execute(sql).fetchone():
                return True
        except sqlite3.Error:
            continue
    return False


def _load_task_names(con):
    try:
        return {int(g): str(n) for g, n in con.execute("SELECT globalTaskId, name FROM COMPUTE_TASK_INFO")}
    except sqlite3.Error:
        return {}


def _load_step_time(con):
    try:
        rows = con.execute("SELECT id, startNs, endNs FROM STEP_TIME ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    return [(int(i), int(s), int(e)) for i, s, e in rows if e > s]


def load_native(out_dir):
    """返回 native 侧数据 dict；无 db 时返回 None。

    只要 db 含 STEP_TIME / PYTORCH_API / CANN_API / TASK 任意一项就使用：
    STEP_TIME 为空（例如由 kernel_details.csv 分步）时，精确配对数据仍要保留。
    """
    best = None
    best_rank = (-1, -1)
    for db in _find_dbs(out_dir):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        steps = _load_step_time(con)
        rank = (1 if steps else 0, 1 if _has_metric_data(con) else 0)
        if rank > best_rank:
            if best is not None:
                best[1].close()
            best = (db, con, steps)
            best_rank = rank
        else:
            con.close()
    if best is None:
        return None
    db, con, steps = best
    try:
        names = _load_string_ids(con)
        conn_map = _load_conn_map(con)
        tasks = _load_tasks(con)
        task_intervals = _load_task_intervals(con)
        task_names = _load_task_names(con)

        def resolve(conn_id):
            if conn_id is None:
                return None
            cid = conn_map.get(int(conn_id), int(conn_id))
            return tasks.get(cid)

        pairs = []
        try:
            op_rows = con.execute(
                "SELECT startNs, endNs, connectionId, name FROM PYTORCH_API WHERE type = 50001"
            ).fetchall()
        except sqlite3.Error:
            op_rows = []
        queue_rows = []
        try:
            queue_rows = con.execute(
                "SELECT startNs, endNs, connectionId, name FROM PYTORCH_API WHERE type = 50002"
            ).fetchall()
        except sqlite3.Error:
            pass
        queue_ends = {}
        enqueue_db = []
        for s, e, conn_id, nid in queue_rows:
            if int(e) <= int(s):
                continue
            enqueue_db.append(
                {
                    "name": names.get(int(nid), str(nid)).split("@", 1)[-1],
                    "start_ns": int(s),
                    "dur_ms": round((int(e) - int(s)) / 1e6, 3),
                }
            )
            if conn_id is not None:
                cid = conn_map.get(int(conn_id), int(conn_id))
                queue_ends.setdefault(cid, []).append(int(e))

        def enqueue_end_for(cid, task_start):
            ends = sorted(queue_ends.get(cid, []))
            if not ends:
                return None
            before = [x for x in ends if x <= task_start]
            # 没有“不晚于设备启动”的 enqueue 结束时间时返回 None，由调用方回退到
            # op 结束口径；不能返回 ends[0]（必然晚于设备启动，会制造负延迟）。
            return before[-1] if before else None

        for start, end, conn_id, name_id in op_rows:
            task = resolve(conn_id)
            if not task:
                continue
            task_start, gid = task
            op_name = names.get(int(name_id), str(name_id))
            cid = conn_map.get(int(conn_id), int(conn_id)) if conn_id is not None else None
            enq_end = enqueue_end_for(cid, int(task_start)) if cid is not None else None
            ref_end = enq_end if enq_end is not None else int(end)
            pairs.append(
                {
                    "op": op_name,
                    "task": task_names.get(gid, ""),
                    "enqueue_end_ns": int(ref_end),
                    "op_end_ns": int(end),
                    "delay_basis": "enqueue" if enq_end is not None else "op",
                    "device_start_ns": int(task_start),
                    "delay_ms": round((int(task_start) - int(ref_end)) / 1e6, 3),
                    "op_delay_ms": round((int(task_start) - int(end)) / 1e6, 3),
                }
            )

        launch_rows = []
        try:
            launch_rows = con.execute(
                "SELECT startNs, endNs, connectionId, type, name FROM CANN_API "
                "WHERE type IN (10000, 20000)"
            ).fetchall()
        except sqlite3.Error:
            pass
        launches = []
        for start, end, conn_id, typ, nid in launch_rows:
            if resolve(conn_id) is None:
                continue
            launches.append(
                {
                    "api": names.get(int(nid), str(nid)),
                    "start_ns": int(start),
                    "dur_ms": round((int(end) - int(start)) / 1e6, 3),
                    "type": int(typ),
                }
            )
        return {
            "db": Path(db).name,
            "steps": steps,
            "pairs": pairs,
            "enqueue_db": enqueue_db,
            "launches": launches,
            "task_intervals": task_intervals,
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# step 边界（无 STEP_TIME 时）
# ---------------------------------------------------------------------------
def _detect_steps(trace_path, events, out_dir):
    db = None
    for f in _find_dbs(out_dir):
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        st = _load_step_time(con)
        con.close()
        if st:
            db = st
            break
    if db:
        return [(i, s, e) for i, s, e in db], "STEP_TIME"
    path = out_dir / "kernel_details.csv"
    if path.exists():
        import csv

        groups = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                try:
                    sid = int((r.get("Step Id") or "").strip())
                    start = float((r.get("Start Time(us)") or "").strip()) * 1000
                    dur = float((r.get("Duration(us)") or "0").strip()) * 1000
                except (TypeError, ValueError):
                    continue
                g = groups.setdefault(sid, [start, start + dur])
                g[0] = min(g[0], start)
                g[1] = max(g[1], start + dur)
        if groups:
            return [(sid, int(groups[sid][0]), int(groups[sid][1])) for sid in sorted(groups)], "kernel_details"
    if trace_path:
        detect = None
        for cand in (
            SKILLS_DIR / "kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py",
            *SKILLS_DIR.glob("*/kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py"),
        ):
            if Path(cand).exists():
                spec = importlib.util.spec_from_file_location("hd_detect_steps", cand)
                detect = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(detect)
                break
        if detect is not None:
            try:
                res = detect.detect(str(trace_path))
                steps = [(s["id"], s["start_ns"], s["end_ns"]) for s in res["steps"]]
                if steps:
                    return steps, f"detect_steps({res.get('source')})"
            except Exception:
                pass
    lo = min(e[0] for e in events) if events else 0
    his = [e[0] + e[1] for e in events if e[1] > 0]
    hi = max(his) if his else lo
    return [(0, lo, hi)], "window"


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _step_of(steps, t):
    for sid, s, e in steps:
        if s <= t < e:
            return sid
    return steps[-1][0] if steps else 0


def analyze(path, skip_warmup=0, pair_window_ms=2.0, top_n=10):
    out_dir, trace_path = locate_capture(path)
    events, host_pid, ui_zero = load_trace(trace_path)
    notes = []
    degradations = []
    sidecars = [trace_path.name] if trace_path else []
    if trace_path is None:
        degradations.append("未找到 trace_view.json，仅用 db 侧车统计")
    if not events:
        degradations.append("trace 不可用或为空，enqueue/dequeue 精确配对不可用")

    native = load_native(out_dir)
    launches = native["launches"] if native else []
    if native:
        sidecars.append(native["db"])
    steps, step_source = _detect_steps(trace_path, events, out_dir)
    if skip_warmup > 0:
        steps = steps[skip_warmup:]
        if not steps:
            degradations.append("--skip-warmup 后没有剩余 step，统计窗口为空")
    win_lo = steps[0][1] if steps else None
    win_hi = steps[-1][2] if steps else None
    if len(steps) <= 1 and step_source == "window":
        degradations.append("无可靠 step 边界，按整窗统计")

    # ---- enqueue ----
    enqueue = []
    for ev in events:
        if ev[3] != "enqueue" or ev[1] <= 0:
            continue
        name = ev[2].split("@", 1)[-1]
        enqueue.append(
            {"name": name, "start_ns": ev[0], "end_ns": ev[0] + ev[1],
             "dur_ms": round(_ms(ev[1]), 3), "corr": ev[6].get("correlation_id")}
        )
    enqueue_source = "trace_view.json:enqueue"
    if not enqueue and native and native["enqueue_db"]:
        enqueue = [
            {"name": e["name"], "start_ns": e["start_ns"], "end_ns": e["start_ns"] + int(e["dur_ms"] * 1e6),
             "dur_ms": e["dur_ms"], "corr": None}
            for e in native["enqueue_db"]
        ]
        enqueue_source = f"{native['db']}:PYTORCH_API(queue)"
    elif not enqueue:
        degradations.append("无 enqueue 事件，enqueue 时延统计不可用")
    if enqueue:
        sidecars.append(enqueue_source)
    # --skip-warmup 显式要求裁剪时，全局指标与 per_step 必须同口径；
    # 未指定 warmup 时保留全量，但对 step 窗口外的事件显式分类（不静默丢弃）。
    trim = skip_warmup > 0
    outside_enqueue = 0
    if win_lo is not None and win_hi is not None:
        kept = []
        for e in enqueue:
            if win_lo <= e["start_ns"] < win_hi:
                kept.append(e)
            else:
                outside_enqueue += 1
        if trim:
            enqueue = kept
        if trim:
            launches = [
                x for x in launches
                if win_lo <= x.get("start_ns", win_lo) < win_hi
            ]

    # ---- queue wait（enqueue → dequeue，按 correlation_id）----
    deq = {}
    for ev in events:
        if ev[3] != "dequeue" or ev[1] <= 0:
            continue
        corr = ev[6].get("correlation_id")
        if corr is not None:
            deq[corr] = min(deq.get(corr, ev[0]), ev[0])
    queue_wait = []
    for e in enqueue:
        start = deq.get(e.get("corr"))
        if start is not None:
            queue_wait.append(round(_ms(start - e["end_ns"]), 3))
    if enqueue and not queue_wait:
        degradations.append("enqueue 与 dequeue 的 correlation_id 未配对，队列等待时延缺失")

    # ---- dispatch delay（enqueue/op → device start）----
    pairs = []
    pair_source = None
    if native and native["pairs"]:
        pairs = native["pairs"]
        pair_source = f"{native['db']}:PYTORCH_API→CONNECTION_IDS→TASK"
        if any(p.get("delay_basis") == "enqueue" for p in pairs):
            notes.append("db 下发延迟优先用实际 enqueue(50002) 结束时间；无 enqueue 记录时回退 op 结束（见 dispatch_delay_basis 的计数与分位数）")
        sidecars.append(pair_source)
    else:
        # trace-only：dequeue → 其后第一个设备任务，时间窗内最近邻。
        window_ns = int(pair_window_ms * 1e6)
        dev = sorted(
            (ev[0], ev[2]) for ev in events if is_device_task(ev, host_pid)
        )
        dev_starts = [s for s, _ in dev]
        matched = 0
        for e in enqueue:
            dq = deq.get(e.get("corr"))
            if dq is None:
                continue
            # 设备任务已按 start 排序：距 dq 最近的候选就是第一个 start >= dq 的任务。
            idx = bisect.bisect_left(dev_starts, dq)
            best = None
            if idx < len(dev) and dev[idx][0] - dq <= window_ns:
                best = dev[idx]
            if best:
                matched += 1
                pairs.append(
                    {"op": e["name"], "task": best[1], "enqueue_end_ns": e["end_ns"],
                     "device_start_ns": best[0], "delay_ms": round(_ms(best[0] - e["end_ns"]), 3),
                     "delay_basis": "enqueue"}
                )
        if pairs:
            pair_source = "trace_view.json:dequeue→device(启发式)"
            sidecars.append(pair_source)
        elif deq:
            degradations.append("trace 启发式未能把 dequeue 配对到设备任务，下发→启动延迟缺失")

    # 配对同样按 step 窗口分类；仅 --skip-warmup 显式裁剪时才真正丢弃。
    outside_pairs = 0
    if win_lo is not None and win_hi is not None:
        kept = []
        for p in pairs:
            if win_lo <= p.get("device_start_ns", win_lo) < win_hi:
                kept.append(p)
            else:
                outside_pairs += 1
        if trim:
            pairs = kept
    # 负延迟表示设备在参考点（enqueue/op 结束）前已启动，属正常重叠情形；
    # 单列计数并从分位数/Top/摘要中剔除，避免把正常下发报成负延迟。
    negative_pairs = [p for p in pairs if p.get("delay_ms", 0) < 0]
    valid_pairs = [p for p in pairs if p.get("delay_ms", 0) >= 0]

    pair_rate = 0.0
    if enqueue:
        pair_rate = round(min(1.0, len(valid_pairs) / len(enqueue)), 3)
    if valid_pairs and pair_rate < 0.3:
        degradations.append(f"下发→设备配对率仅 {pair_rate:.0%}，延迟统计只代表可配对子集")
    if outside_enqueue or outside_pairs:
        if trim:
            notes.append(
                f"按 --skip-warmup 裁剪：step 窗口外 enqueue {outside_enqueue} 条、"
                f"配对 {outside_pairs} 条已从全局指标与 per_step 同步排除"
            )
        else:
            notes.append(
                f"有 enqueue {outside_enqueue} 条、配对 {outside_pairs} 条落在 step 窗口外："
                "已计入全局指标、但不参与 per_step（未指定 --skip-warmup，不做全局裁剪）"
            )
    if negative_pairs:
        notes.append(
            f"{len(negative_pairs)} 个配对的下发→启动延迟为负"
            "（设备在 enqueue/op 结束前已启动），已从分位数与 Top 中剔除"
        )

    # ---- per step ----
    enqueue_by_step = {}
    for e in enqueue:
        enqueue_by_step.setdefault(_step_of(steps, e["start_ns"]), []).append(e)
    pairs_by_step = {}
    for p in valid_pairs:
        pairs_by_step.setdefault(_step_of(steps, p["enqueue_end_ns"]), []).append(p)
    dev_ivs = [(ev[0], ev[0] + ev[1]) for ev in events if is_device_task(ev, host_pid)]
    if not dev_ivs and native:
        # 仅有 db 时用 TASK 起止区间补设备忙碌，避免重叠率恒为 0。
        dev_ivs = list(native.get("task_intervals") or [])
    dev_ivs_available = bool(dev_ivs)

    per_step = []
    for sid, start, end in steps:
        es = [e for e in enqueue_by_step.get(sid, []) if start <= e["start_ns"] < end]
        ps = [p for p in pairs_by_step.get(sid, []) if start <= p["enqueue_end_ns"] < end]
        host_ivs = [(e["start_ns"], e["end_ns"]) for e in es]
        host_total = cover(host_ivs, start, end)
        overlap = (
            intersect_cover(host_ivs, dev_ivs, start, end) if dev_ivs_available else None
        )
        per_step.append(
            {
                "step": sid,
                "enqueue_count": len(es),
                "enqueue_ms": round(sum(e["dur_ms"] for e in es), 3),
                "dispatch_pairs": len(ps),
                "dispatch_ms": round(sum(p["delay_ms"] for p in ps), 3),
                "overlap_ratio": (
                    round(overlap / host_total, 3)
                    if (overlap is not None and host_total > 0)
                    else None
                ),
                "exposed_ms": (
                    round((host_total - overlap) / 1e6, 3)
                    if overlap is not None
                    else None
                ),
            }
        )
    # 去掉没有数据的 warmup/尾部空步
    per_step = [s for s in per_step if s["enqueue_count"] or s["dispatch_pairs"]]

    # ---- Top ops ----
    by_op = {}
    for e in enqueue:
        slot = by_op.setdefault(
            e["name"], {"name": e["name"], "count": 0, "enqueue_ms": 0.0, "enq_durs": [], "delays": []}
        )
        slot["count"] += 1
        slot["enqueue_ms"] += e["dur_ms"]
        slot["enq_durs"].append(e["dur_ms"])
    for p in valid_pairs:
        slot = by_op.setdefault(
            p["op"], {"name": p["op"], "count": 0, "enqueue_ms": 0.0, "enq_durs": [], "delays": []}
        )
        slot["delays"].append(p["delay_ms"])
    top_enqueue = sorted(
        [v for v in by_op.values() if v["enq_durs"]], key=lambda x: x["enqueue_ms"], reverse=True
    )[:top_n]
    top_delay = sorted(
        [v for v in by_op.values() if v["delays"]],
        key=lambda x: max(x["delays"]),
        reverse=True,
    )[:top_n]

    def op_rows(items):
        return [
            {
                "name": v["name"],
                "count": v["count"],
                "enqueue_ms": round(v["enqueue_ms"], 3),
                "enqueue_p50_ms": pct(v["enq_durs"], 0.5),
                "enqueue_p95_ms": pct(v["enq_durs"], 0.95),
                "enqueue_p99_ms": pct(v["enq_durs"], 0.99),
                "delay_ms": round(sum(v["delays"]), 3) if v["delays"] else None,
                "delay_max_ms": round(max(v["delays"]), 3) if v["delays"] else None,
            }
            for v in items
        ]

    by_launch = {}
    for x in launches:
        slot = by_launch.setdefault(
            x["api"], {"name": x["api"], "count": 0, "dur_ms": 0.0, "max_ms": 0.0}
        )
        slot["count"] += 1
        slot["dur_ms"] += x["dur_ms"]
        slot["max_ms"] = max(slot["max_ms"], x["dur_ms"])
    top_launch = sorted(by_launch.values(), key=lambda x: x["dur_ms"], reverse=True)[:top_n]

    def launch_rows(items):
        return [
            {
                "name": v["name"],
                "count": v["count"],
                "launch_ms": round(v["dur_ms"], 3),
                "launch_max_ms": round(v["max_ms"], 3),
            }
            for v in items
        ]

    enq_durs = [e["dur_ms"] for e in enqueue]
    delays = [p["delay_ms"] for p in valid_pairs]
    total_host = cover(
        [(e["start_ns"], e["end_ns"]) for e in enqueue],
        steps[0][1] if steps else 0,
        steps[-1][2] if steps else 0,
    )
    if dev_ivs_available:
        total_overlap = intersect_cover(
            [(e["start_ns"], e["end_ns"]) for e in enqueue], dev_ivs,
            steps[0][1] if steps else 0, steps[-1][2] if steps else 0,
        )
        overlap_ratio = round(total_overlap / total_host, 3) if total_host > 0 else None
        exposed_total = round(max(0, total_host - total_overlap) / 1e6, 3)
    else:
        overlap_ratio = None
        exposed_total = None
    overlap_evidence = (
        "enqueue 区间 ∩ 设备任务区间 / enqueue 总时长"
        if dev_ivs_available
        else "设备任务区间缺失（trace/TASK 均无起止），指标不可用"
    )
    launch_durs = [x["dur_ms"] for x in launches]
    queue_evidence = (
        "trace_view.json:enqueue/dequeue(correlation_id)" if queue_wait else "未配对或不可用"
    )

    metrics = [
        {"name": "enqueue_count", "unit": "count", "base": len(enqueue), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": enqueue_source},
        {"name": "enqueue_p50_ms", "unit": "ms", "base": pct(enq_durs, 0.5), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": enqueue_source},
        {"name": "enqueue_p95_ms", "unit": "ms", "base": pct(enq_durs, 0.95), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": enqueue_source},
        {"name": "enqueue_p99_ms", "unit": "ms", "base": pct(enq_durs, 0.99), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": enqueue_source},
        {"name": "queue_wait_p50_ms", "unit": "ms", "base": pct(queue_wait, 0.5), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": queue_evidence},
        {"name": "queue_wait_p95_ms", "unit": "ms", "base": pct(queue_wait, 0.95), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": queue_evidence},
        {"name": "dispatch_delay_p50_ms", "unit": "ms", "base": pct(delays, 0.5), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": pair_source or "无配对"},
        {"name": "dispatch_delay_p95_ms", "unit": "ms", "base": pct(delays, 0.95), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": pair_source or "无配对"},
        {"name": "dispatch_delay_p99_ms", "unit": "ms", "base": pct(delays, 0.99), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": pair_source or "无配对"},
        {"name": "pair_rate", "unit": "ratio", "base": pair_rate, "cand": None,
         "delta": None, "delta_pct": None,
         "verdict": "info" if pair_rate >= 0.3 else "regressed", "noise_band": None,
         "evidence": pair_source or "无配对"},
        {"name": "host_device_overlap_ratio", "unit": "ratio", "base": overlap_ratio, "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": overlap_evidence},
        {"name": "exposed_dispatch_ms", "unit": "ms", "base": exposed_total, "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": (
             "enqueue 区间中不与设备任务重叠的时长（下发阻塞/暴露在关键路径）"
             if dev_ivs_available else "设备任务区间缺失（trace/TASK 均无起止），指标不可用"
         )},
        {"name": "launch_count", "unit": "count", "base": len(launches), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": "db:CANN_API(type=10000/20000)" if launches else "无 launch 数据"},
        {"name": "launch_p50_ms", "unit": "ms", "base": pct(launch_durs, 0.5), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": "db:CANN_API(type=10000/20000)" if launches else "无 launch 数据"},
        {"name": "launch_p95_ms", "unit": "ms", "base": pct(launch_durs, 0.95), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": "db:CANN_API(type=10000/20000)" if launches else "无 launch 数据"},
        {"name": "launch_p99_ms", "unit": "ms", "base": pct(launch_durs, 0.99), "cand": None,
         "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
         "evidence": "db:CANN_API(type=10000/20000)" if launches else "无 launch 数据"},
    ]

    conf = "high"
    if not enqueue or not pairs:
        conf = "medium" if (enqueue or pairs) else "low"
    if pair_rate < 0.3 or step_source in ("window", "detect_steps(trace)"):
        conf = "low" if not pairs else "medium"
    # trace 启发式配对不是精确配对：即使配对率达 30%、步源是 kernel_details，也最多 medium。
    if pair_source and "启发式" in pair_source and conf == "high":
        conf = "medium"

    notes.append(
        "enqueue 时延是单次入队调用时长；enqueue 很短不代表 host 不慢，"
        "瓶颈常在下发频率、锁竞争或 host 计算占用。"
    )
    notes.append(
        "dispatch_delay 含设备排队等待，不是纯 host 开销；"
        "判断 host 是否瓶颈应结合 enqueue_* 与 queue_wait_*。"
    )
    if pair_source and "启发式" in pair_source:
        notes.append(
            "trace-only 的 dequeue→设备任务为时间窗最近邻启发式，可能错配相邻算子；以 pair_rate 为准。"
        )
    notes.append("多 rank 采集的 TASK.startNs 属各自时钟域，不可跨 rank 相减。")

    def _basis_stat(basis):
        allv = [p["delay_ms"] for p in pairs if p.get("delay_basis") == basis]
        pos = [v for v in allv if v >= 0]
        return {
            "count": len(allv),
            "negative": len(allv) - len(pos),
            "p50_ms": pct(pos, 0.5),
            "p95_ms": pct(pos, 0.95),
        }

    dispatch_delay_basis = {
        "enqueue": _basis_stat("enqueue"),
        "op": _basis_stat("op"),
        "negative_excluded": len(negative_pairs),
    }
    if dispatch_delay_basis["op"]["count"]:
        notes.append(
            "部分配对无可用 enqueue 结束时间，回退 op 结束口径，"
            "对应分位数见 dispatch_delay_basis.op（与 enqueue 口径不可直接混比）"
        )

    return {
        "skill": "kptools-ascend-profiling-host-dispatch",
        "schema_version": 1,
        "mode": "single",
        "inputs": {"base": str(Path(path).resolve()), "cand": None, "steps": step_source},
        "data_quality": {"sidecars": sidecars, "degraded": degradations, "confidence": conf},
        "metrics": metrics,
        "per_step": per_step,
        "dispatch_delay_basis": dispatch_delay_basis,
        "outside_window": {"enqueue": outside_enqueue, "pairs": outside_pairs},
        "per_op": {
            "top_enqueue": op_rows(top_enqueue),
            "top_delay": op_rows(top_delay),
            "top_launch": launch_rows(top_launch),
        },
        "charts": {
            "enqueue_hist": hist_bins(enq_durs),
            "dispatch_hist": hist_bins(delays),
        },
        "summary": (
            f"{len(enqueue)} 次下发，enqueue p50 {pct(enq_durs, 0.5)}ms，"
            f"下发→启动 p50 {pct(delays, 0.5)}ms，配对率 {pair_rate:.0%}"
        ),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s), quote=True)


def _css():
    try:
        return CSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return "body{font-family:sans-serif;margin:24px}"


def _table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def hist_svg(bins, title):
    if not bins:
        return ""
    counts = bins["counts"]
    n = sum(counts)
    w, h, pad = 480, 150, 26
    bw = (w - 2 * pad) / len(counts)
    top = max(counts) or 1
    bars = []
    for i, c in enumerate(counts):
        bh = (h - 2 * pad) * c / top
        bars.append(
            f'<rect x="{pad + i * bw:.1f}" y="{h - pad - bh:.1f}" '
            f'width="{max(bw - 2, 1):.1f}" height="{bh:.1f}" fill="#4a90d9"/>'
        )
    return (
        f'<figure><figcaption>{esc(title)}（n={n}）</figcaption>'
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">' + "".join(bars)
        + f'<text x="{pad}" y="{h - 8}" font-size="10">{bins["lo"]}ms</text>'
        + f'<text x="{w - pad}" y="{h - 8}" font-size="10" text-anchor="end">{bins["hi"]}ms</text>'
        + "</svg></figure>"
    )


def render_html(result):
    mrows = [
        [m["name"], m["base"] if m["base"] is not None else "-", m["unit"], m["evidence"]]
        for m in result["metrics"]
    ]
    srows = [
        [s["step"], s["enqueue_count"], s["enqueue_ms"], s["dispatch_pairs"], s["dispatch_ms"],
         s["overlap_ratio"] if s["overlap_ratio"] is not None else "-", s["exposed_ms"]]
        for s in result["per_step"]
    ]
    def op_table(items):
        return _table(
            ["算子", "次数", "enqueue 总时长(ms)", "p50(ms)", "p95(ms)", "p99(ms)",
             "下发延迟合计(ms)", "最大延迟(ms)"],
            [[i["name"], i["count"], i["enqueue_ms"], i["enqueue_p50_ms"], i["enqueue_p95_ms"],
              i["enqueue_p99_ms"],
              i["delay_ms"] if i["delay_ms"] is not None else "-",
              i["delay_max_ms"] if i["delay_max_ms"] is not None else "-"] for i in items],
        )
    def launch_table(items):
        return _table(
            ["launch API", "次数", "launch 总时长(ms)", "最大(ms)"],
            [[i["name"], i["count"], i["launch_ms"], i["launch_max_ms"]] for i in items],
        )
    charts = result.get("charts") or {}
    hist_html = (
        hist_svg(charts.get("enqueue_hist"), "enqueue 时延分布")
        + hist_svg(charts.get("dispatch_hist"), "下发→启动延迟分布")
    )
    degraded = "".join(f"<li>{esc(d)}</li>" for d in result["data_quality"]["degraded"]) or "<li>无</li>"
    notes = "".join(f"<li>{esc(n)}</li>" for n in result["notes"]) or "<li>无</li>"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Host 下发耗时</title><style>{_css()}</style></head><body><div class="page on">
<h1>Host 下发（enqueue/launch）耗时</h1>
<div class="sect"><p class="sem">输入：{esc(result["inputs"]["base"])}</p>
<p class="sem">{esc(result["summary"])}；置信度 <b>{esc(result["data_quality"]["confidence"])}</b></p>
<p class="sem">step 来源：{esc(result["inputs"]["steps"])}</p></div>
<h2 class="s-P1">判定指标</h2><div class="sect">{_table(["指标","值","单位","依据"], mrows)}</div>
<h2 class="s-P2">下发时延分布</h2><div class="sect">{hist_html or '<p class="sem">无可用样本</p>'}</div>
<h2 class="s-P2">每 step 下发</h2><div class="sect">{_table(["step","下发数","enqueue(ms)","配对下发","延迟合计(ms)","重叠比","阻塞/暴露(ms)"], srows)}</div>
<h2 class="s-P2">下发耗时 Top</h2><div class="sect">{op_table(result["per_op"]["top_enqueue"])}</div>
<h2 class="s-P2">下发→设备启动延迟 Top</h2><div class="sect">{op_table(result["per_op"]["top_delay"])}</div>
<h2 class="s-P2">launch 耗时 Top</h2><div class="sect">{launch_table(result["per_op"].get("top_launch", []))}</div>
<h2 class="s-P3">降级项</h2><div class="sect"><ul>{degraded}</ul></div>
<h2 class="s-P3">口径与限制</h2><div class="sect"><ul>{notes}</ul>
<p class="sem">enqueue 时延来自 enqueue 事件（或 db queue 表）；下发→启动延迟优先用 db connectionId 精确配对，trace-only 时为时间窗最近邻启发式，配对率已标出。重叠比 = enqueue 区间与设备任务区间的交集 / enqueue 总时长。</p></div>
</div></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Host 下发耗时统计")
    ap.add_argument("capture")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--out-html", dest="html_path")
    ap.add_argument("--skip-warmup", type=int, default=0)
    ap.add_argument("--pair-window-ms", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)
    try:
        result = analyze(args.capture, args.skip_warmup, args.pair_window_ms, args.top)
    except Exception as exc:
        if args.html_path:
            Path(args.html_path).write_text(
                f"<!doctype html><meta charset='utf-8'><h1>Host 下发分析失败</h1>"
                f"<p>{esc(exc)}</p>", encoding="utf-8")
        print(f"错误: {exc}", file=sys.stderr)
        return 3
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.html_path:
        Path(args.html_path).write_text(render_html(result), encoding="utf-8")
    print(result["summary"])
    for d in result["data_quality"]["degraded"]:
        print(f"  degrade: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
