#!/usr/bin/env python3
"""无标识 trace 的深度 step 切分。

目标场景：trace 里没有可用的 ProfilerStep#N / STEP_TIME 侧车，或既有切分明显不准。
做法：多信号投票（设备 sync、设备忙闲周期、host enqueue 批次周期、idle gap 分层），
每个边界输出置信度与依据；信号冲突/证据不足时显式标 uncertain，而不是硬切。

输入：
  trace_view.json（必需）
  可选侧车（同目录或 *ascend_pt 根目录）：
    ascend_pytorch_profiler_*.db  STEP_TIME   权威 step 边界（自检模式）
    kernel_details.csv            Step Id     设备侧权威 step 起点（自检模式）

输出：
  --json 家族统一信封（skill/schema_version/mode/inputs/data_quality/metrics/per_step/summary/notes，mode=single）+ boundaries/self_check
  --out-html HTML 报告

用法：
  python3 scripts/split_steps.py <trace_view.json> --json out.json --out-html report.html
"""

import argparse
import bisect
import glob
import html
import json
import sqlite3
import statistics
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
CSS_PATH = SKILL_DIR / "assets" / "report.css"

OA_NAMES = {"Computing", "Communication", "Communication(Not Overlapped)", "Free"}
HOST_CATS = {"cpu_op", "enqueue", "dequeue"}
AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}

# 信号权重：sync 是设备侧强信号；周期是结构性信号；host 批次和 idle gap 为辅助。
SIGNAL_WEIGHTS = {"idle": 0.55, "sync": 0.30, "period": 0.25, "host": 0.15}


# ---------------------------------------------------------------------------
# trace 读取
# ---------------------------------------------------------------------------
def _ns(value):
    return int(round(float(value) * 1000.0))


def locate_trace(path):
    """接受 trace_view.json、ASCEND_PROFILER_OUTPUT 或 capture 目录。"""
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return p
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    direct = p / "ASCEND_PROFILER_OUTPUT" / "trace_view.json"
    if direct.exists():
        return direct
    if (p / "trace_view.json").exists():
        return p / "trace_view.json"
    hits = sorted(p.rglob("trace_view.json"))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"未找到 trace_view.json: {p}")


def load_trace(trace_path):
    """返回 events=[(start_ns,end_ns,name,cat,pid,tid,args)], host_pid, ui_zero。"""
    path = Path(trace_path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("traceEvents") or data.get("events") or []
    events = []
    ui_zero = None
    for e in data:
        if not isinstance(e, dict):
            continue
        if ui_zero is None and e.get("ts") is not None:
            ui_zero = _ns(e["ts"])
        if e.get("ph") != "X" or e.get("ts") is None:
            continue
        dur = e.get("dur") or 0
        start = _ns(e["ts"])
        events.append(
            (
                start,
                _ns(dur) if dur else 0,
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
    # 没有 host 证据时保持 None：首个事件可能是设备 pid，把它当 host 会让
    # 该 pid 上的全部设备任务被 is_device_task 排除，误降级为 host-only。
    host_pid = max(host_count, key=host_count.get) if host_count else None
    return events, host_pid, ui_zero or 0


def is_sync(ev):
    return "Synchronize" in ev[2] and ev[1] > 0


def is_oa(ev):
    return ev[2] in OA_NAMES


def is_device_task(ev, host_pid):
    _, dur, name, cat, pid, _, args = ev
    if dur <= 0 or pid == host_pid or is_oa(ev) or is_sync(ev):
        return False
    if "ProfilerStep#" in name:
        return False
    return ("task id" in args) or (cat in AI_TASK_TYPES) or (name in ("Memcpy", "Memset"))


def union(interval_list):
    out = []
    for a, b in sorted((int(s), int(e)) for s, e in interval_list if e > s):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def host_intervals(events, host_pid):
    """host 活动区间；ProfilerStep# 与 Overlap Analysis 事件不参与识别。"""
    if host_pid is None:
        chosen = [ev for ev in events if ev[3] in HOST_CATS]
    else:
        chosen = [ev for ev in events if ev[4] == host_pid]
    return union(
        [
            (ev[0], ev[0] + ev[1])
            for ev in chosen
            if ev[1] > 0 and not ev[2].startswith("ProfilerStep#") and not is_oa(ev)
        ]
    )


def gaps_of(intervals, lo, hi):
    """区间并集的补集（只返回 lo..hi 内的 gap）。"""
    out = []
    prev = lo
    for s, e in union(intervals):
        if e <= lo:
            continue
        if s >= hi:
            break
        if s > prev:
            out.append((prev, min(s, hi)))
        prev = max(prev, e)
        if prev >= hi:
            break
    if prev < hi:
        out.append((prev, hi))
    return out


def overlaps(a, b, s, e):
    return a < e and s < b


def _cluster_times(times, gap_ns):
    clusters = []
    for t in sorted(times):
        if clusters and t - clusters[-1][-1] <= gap_ns:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [c[len(c) // 2] for c in clusters]


def has_activity(events, a, b, host_pid):
    """窗口内是否有真实设备活动（用于剔除退出阶段的 sync）。"""
    for ev in events:
        if ev[1] <= 0 or not is_device_task(ev, host_pid):
            continue
        if overlaps(a, b, ev[0], ev[0] + ev[1]):
            return True
    return False


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------
def device_episodes(events, host_pid, preserve_gap_ns=0):
    """把设备 busy 区间按小 gap 合并成执行片段，返回 (episodes, gaps)。

    合并阈值来自典型空档的 2 倍，但**不得吞掉达到切步阈值的空档**：
    若所有相邻迭代空档都是 2ms 而 min_step 是 0.5ms，这些空档必须保留为边界候选。
    gaps 为片段之间的空档 (start_ns, end_ns, size_ns)；start 即上一个片段的结束。
    """
    busy = union([(ev[0], ev[0] + ev[1]) for ev in events if is_device_task(ev, host_pid)])
    if not busy:
        return [], []
    raw_gaps = [busy[i + 1][0] - busy[i][1] for i in range(len(busy) - 1)]
    raw_gaps = [g for g in raw_gaps if g > 0]
    merge_ns = int(max(0.1e6, statistics.median(raw_gaps) * 2)) if raw_gaps else int(0.1e6)
    episodes = []
    for s, e in busy:
        gap = s - episodes[-1][1] if episodes else float("inf")
        if episodes and gap < merge_ns and (preserve_gap_ns <= 0 or gap < preserve_gap_ns):
            episodes[-1][1] = max(episodes[-1][1], e)
        else:
            episodes.append([s, e])
    gaps = []
    for i in range(len(episodes) - 1):
        start, end = episodes[i][1], episodes[i + 1][0]
        if end > start:
            gaps.append((start, end, end - start))
    return [(int(s), int(e)) for s, e in episodes], gaps


def idle_signal(gaps, min_gap_ns):
    """设备空档分层：只有不小于 min_gap_ns 的空档才作为 step 边界候选。

    边界取空档起点（上一个 step 最后一个设备任务结束处），与 STEP_TIME 的
    step 起点口径一致（实测差 < 0.3ms）。
    """
    out = []
    for start, _end, size in gaps:
        if size < min_gap_ns:
            continue
        score = min(1.0, max(0.35, size / float(min_gap_ns * 3)))
        out.append(
            {
                "t": start,
                "score": round(score, 3),
                "evidence": f"设备空档 {size / 1e6:.3f}ms ≥ {min_gap_ns / 1e6:.3f}ms",
            }
        )
    return out


def sync_signal(events, starts, tol_ns):
    """只保留落在候选空档起点附近的 sync 结束，避免把每步内的 sync 当边界。"""
    if not starts:
        return []
    ordered = sorted(starts)
    times = []
    for ev in events:
        if not is_sync(ev):
            continue
        end = ev[0] + ev[1]
        i = bisect.bisect_left(ordered, end - tol_ns)
        if i < len(ordered) and ordered[i] <= end + tol_ns:
            times.append(end)
    out = []
    for t in _cluster_times(times, tol_ns):
        out.append({"t": t, "score": 1.0, "evidence": "sync 结束与设备空档起点对齐"})
    return out


def sync_fallback(events, lo, hi, min_step_ns):
    """无设备空档与 host 批次时，用周期性 sync 结束做兜底边界。

    只在 sync 结束序列呈稳定周期时启用，避免把 step 内的零散 sync 当边界；
    与空档对齐的 sync 仍由 sync_signal 负责佐证。
    """
    times = sorted(
        {int(ev[0] + ev[1]) for ev in events if is_sync(ev) and lo < ev[0] + ev[1] < hi}
    )
    if len(times) < 3:
        return []
    deltas = [b - a for a, b in zip(times, times[1:])]
    deltas = [d for d in deltas if d >= min_step_ns]
    if len(deltas) < 2:
        return []
    clusters = []
    for d in sorted(deltas):
        if clusters and d <= clusters[-1][-1] * 1.15:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    best = max(clusters, key=len)
    if len(best) < 2:
        return []
    period = int(statistics.median(best))
    if period <= 0:
        return []
    tol = max(int(0.5e6), int(period * 0.1))
    out = []
    for t in times:
        err = min((t - times[0]) % period, period - (t - times[0]) % period)
        if err <= tol:
            score = round(max(0.3, 1.0 - err / float(tol + 1)), 3)
            out.append({"t": t, "score": score,
                        "evidence": f"sync 周期 {period / 1e6:.3f}ms 对齐"})
    return out


def _busy_series(episodes, lo, hi, bin_ns):
    n = max(1, int((hi - lo) / bin_ns) + 1)
    series = [0] * n
    for s, e in episodes:
        a = max(0, int((s - lo) / bin_ns))
        b = min(n - 1, int((e - lo) / bin_ns))
        for i in range(a, b + 1):
            series[i] = 1
    return series


def period_signal(episodes, gaps, lo, hi, min_gap_ns):
    """从空档间隔的重复性检测主导周期（等价于对 burst 序列做自相关）。

    只考虑不小于 3 倍典型空档的间隔，避免 step 内部的 kernel 交替被当成周期。
    返回与周期网格对齐的空档起点；周期不成立时返回空列表。
    """
    if len(gaps) < 4:
        return [], None
    sizes = [size for _start, _end, size in gaps]
    med_gap = statistics.median(sizes)
    min_period = max(int(1.0e6), int(med_gap * 3))
    starts = [start for start, _end, size in gaps if size >= min_gap_ns]
    if len(starts) < 4:
        return [], None
    deltas = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    deltas = [d for d in deltas if d >= min_period]
    if len(deltas) < 2:
        return [], None

    # 按 15% 相对容差聚类间隔，取出现次数最多的簇作为周期。
    clusters = []
    for d in sorted(deltas):
        if clusters and d <= clusters[-1][-1] * 1.15:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    best = max(clusters, key=len)
    if len(best) < 3:
        return [], None
    period = int(statistics.median(best))

    # 用该周期处的忙闲序列自相关作为置信度（低成本校验，不参与搜索）。
    bin_ns = max(int(0.2e6), (hi - lo) // 20000 + 1)
    series = _busy_series(episodes, lo, hi, bin_ns)
    n = len(series)
    lag = max(1, int(round(period / float(bin_ns))))
    corr = 0.0
    if lag < n:
        mean = sum(series) / n
        centered = [x - mean for x in series]
        denom = sum(x * x for x in centered)
        if denom > 0:
            corr = sum(centered[i] * centered[i + lag] for i in range(n - lag)) / denom
    if corr < 0.3:
        return [], None

    out = []
    for start in starts:
        err = abs((start - starts[0]) % period)
        err = min(err, period - err)
        if err <= period * 0.15:
            score = round(max(0.3, corr * (1.0 - err / (period * 0.15))), 3)
            out.append(
                {
                    "t": start,
                    "score": score,
                    "evidence": f"与主导周期 {period / 1e6:.3f}ms 对齐（自相关 {corr:.2f}）",
                }
            )
    return out, period


def host_signal(events, host_pid, lo, hi, min_gap_ns):
    """host 下发批次：host 空闲后恢复下发（且有 enqueue）的时刻作为辅助边界。

    边界统一取 host 空档的**起点**（上一批下发结束处），与 idle_signal 的
    “空档起点”参考点一致，避免同一物理边界被两端各标一次产生幻影 step。
    enqueue 计数用 bisect 在有序数组上做区间查询，避免逐个空档全量扫 events。
    """
    host = host_intervals(events, host_pid)
    if not host:
        return []
    gaps = gaps_of(host, lo, hi)
    enq_starts = sorted(ev[0] for ev in events if ev[3] == "enqueue")
    min_gap = max(min_gap_ns, int(0.5e6))
    starts = []
    for gs, ge in gaps:
        if ge - gs < min_gap:
            continue
        # 恢复后 2ms 内至少 2 个 enqueue，才视为一次真实下发批次。
        begin = bisect.bisect_left(enq_starts, ge)
        end = bisect.bisect_right(enq_starts, ge + int(2e6))
        if end - begin >= 2:
            starts.append(gs)
    if len(starts) < 2:
        return []
    period = statistics.median(starts[i + 1] - starts[i] for i in range(len(starts) - 1)) if len(starts) >= 3 else None
    out = []
    for s in starts:
        score = 0.8
        if period:
            rel = abs(((s - starts[0]) % period))
            rel = min(rel, period - rel) / period
            score = max(0.3, 1.0 - rel * 2)
        out.append({"t": s, "score": round(score, 3), "evidence": "host 空闲后恢复下发批次（空档起点）"})
    return out


# ---------------------------------------------------------------------------
# 融合与切分
# ---------------------------------------------------------------------------
def fuse(candidates, lo, hi, tol_ns, min_step_ns):
    """候选按时间聚类；cluster 置信度 = 出现信号权重和。"""
    cands = []
    for name, items in candidates.items():
        for item in items:
            cands.append((item["t"], name, item["score"], item["evidence"]))
    cands.sort(key=lambda x: x[0])

    clusters = []
    for t, name, score, evidence in cands:
        if clusters and t - clusters[-1]["times"][-1] <= tol_ns:
            clusters[-1]["times"].append(t)
            clusters[-1]["items"].append((name, score, evidence))
        else:
            clusters.append({"times": [t], "items": [(name, score, evidence)]})

    boundaries = []
    for c in clusters:
        signals = {}
        for name, score, evidence in c["items"]:
            prev = signals.get(name)
            if prev is None or score > prev["score"]:
                signals[name] = {"score": score, "evidence": evidence}
        weight = sum(SIGNAL_WEIGHTS[name] * info["score"] for name, info in signals.items())
        confidence = round(min(1.0, weight), 3)
        t = int(statistics.median(c["times"]))
        boundaries.append(
            {
                "t": t,
                "confidence": confidence,
                "signals": sorted(signals),
                "evidence": {name: info["evidence"] for name, info in signals.items()},
                "uncertain": len(signals) < 2,
            }
        )

    boundaries.sort(key=lambda b: b["t"])
    kept = []
    for b in boundaries:
        if b["t"] <= lo or b["t"] >= hi:
            continue
        if kept and b["t"] - kept[-1]["t"] < min_step_ns:
            if b["confidence"] > kept[-1]["confidence"]:
                kept[-1] = b
            continue
        kept.append(b)
    return kept


def build_steps(boundaries, lo, hi, first_authority=False):
    # 与窗口起点重合的权威标记不再重复作为内部边界（避免产生 0 长度前导步）。
    bounds = [lo] + [b["t"] for b in boundaries if b["t"] > lo] + [hi]
    steps = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if end <= start:
            continue
        # 首步起点没有前一边界，置信度只描述“step 起始边界”，故记 0；
        # 权威模式下首步起点本身就是权威边界，记 1。
        if i == 0 and first_authority and boundaries:
            conf, signals, uncertain = 1.0, ["authority"], False
        elif i == 0 or not boundaries:
            conf, signals, uncertain = 0.0, [], True
        else:
            b = boundaries[i - 1]
            conf, signals, uncertain = b["confidence"], b["signals"], b["uncertain"]
        steps.append(
            {
                "step": len(steps),
                "start_ns": start,
                "end_ns": end,
                "confidence": conf,
                "signals": signals,
                "uncertain": uncertain,
            }
        )
    return steps


# ---------------------------------------------------------------------------
# 权威边界与自检
# ---------------------------------------------------------------------------
def authority_boundaries(trace_path, events):
    """返回 (starts_ns, source)。STEP_TIME 优先，其次 ProfilerStep，其次 kernel_details Step Id。"""
    trace_path = Path(trace_path).resolve()
    base = trace_path.parent
    for root in (base, base.parent):
        for db in sorted(glob.glob(str(root / "ascend_pytorch_profiler_*.db"))):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                rows = con.execute("SELECT startNs FROM STEP_TIME ORDER BY id").fetchall()
                con.close()
            except Exception:
                continue
            starts = [int(r[0]) for r in rows if r[0] is not None]
            if len(starts) >= 2:
                return starts, f"{Path(db).name}:STEP_TIME"
    starts = sorted(ev[0] for ev in events if ev[2].startswith("ProfilerStep#") and ev[1] > 0)
    if len(starts) >= 2:
        return starts, "trace_view.json:ProfilerStep#N"
    for root in (base, base.parent):
        path = root / "kernel_details.csv"
        if not path.exists():
            continue
        import csv

        groups = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    try:
                        sid = int((r.get("Step Id") or "").strip())
                        start = float((r.get("Start Time(us)") or "").strip()) * 1000
                    except (TypeError, ValueError):
                        continue
                    groups[sid] = min(groups.get(sid, start), start)
        except OSError:
            continue
        if len(groups) >= 2:
            return [int(groups[k]) for k in sorted(groups)], "kernel_details.csv:Step Id"
    return [], None


def self_check(starts, authority, tol_ns):
    """贪心匹配，返回 precision/recall/F1 与误差。"""
    truth = sorted(authority)
    ours = sorted(starts)
    used = set()
    matched = []
    for t in truth:
        best, best_d = None, None
        for i, s in enumerate(ours):
            if i in used:
                continue
            d = abs(s - t)
            if d <= tol_ns and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            used.add(best)
            matched.append(best_d)
    tp = len(matched)
    precision = tp / len(ours) if ours else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "authority_count": len(truth),
        "our_count": len(ours),
        "matched": tp,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "tolerance_ms": round(tol_ns / 1e6, 3),
        "max_abs_err_ms": round(max(matched) / 1e6, 3) if matched else None,
        "mean_abs_err_ms": round(sum(matched) / len(matched) / 1e6, 3) if matched else None,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def analyze(trace_path, min_step_ms=0.5, min_confidence=0.5, tolerance_ms=2.0,
            force_authority=False, skip_self_check=False):
    trace_path = locate_trace(trace_path)
    events, host_pid, ui_zero = load_trace(trace_path)
    notes = []
    degradations = []
    sidecars = ["trace_view.json"]
    if not events:
        raise ValueError("trace 中没有可用的 X 事件")

    dev_tasks = [ev for ev in events if is_device_task(ev, host_pid)]
    if not dev_tasks:
        degradations.append("未识别到设备任务事件，仅按 host 活动切分")
    lo = min(ev[0] for ev in events)
    hi = max(ev[0] + ev[1] for ev in events if ev[1] > 0)
    if hi <= lo:
        raise ValueError("trace 事件时间轴为空")

    auth_starts, auth_source = authority_boundaries(trace_path, events)
    if auth_starts:
        sidecars.append(auth_source)

    if force_authority:
        if not auth_starts:
            raise ValueError("--authority 要求存在 STEP_TIME / ProfilerStep / kernel_details Step Id")
        auth_in = [t for t in auth_starts if lo <= t < hi]
        if not auth_in:
            auth_in = [t for t in auth_starts if t <= hi]
        boundaries = [
            {"t": t, "confidence": 1.0, "signals": ["authority"], "evidence": {"authority": auth_source},
             "uncertain": False}
            for t in auth_in
        ]
        # 从首个权威起点开始统计，前导区间（如 trace 早于首个 ProfilerStep 的预热）不计为一步。
        lo = auth_in[0] if auth_in else lo
        notes.append(f"权威模式：使用 {auth_source} 的 step 边界，窗口从首个权威起点开始")
    else:
        step_preserve_ns = int(max(0.3e6, min_step_ms * 1e6))
        episodes, gaps = device_episodes(events, host_pid, step_preserve_ns)
        if not episodes:
            degradations.append("未识别到设备任务，退化用 host 忙闲切分（置信度降级）")
            host = host_intervals(events, host_pid)
            episodes = host
            gaps = []
            for i in range(len(host) - 1):
                start, end = host[i][1], host[i + 1][0]
                if end > start:
                    gaps.append((start, end, end - start))
        # 空档阈值：默认 min_step_ms（0.5ms），且不小于 0.3ms 硬下限。
        min_gap_ns = int(max(0.3e6, min_step_ms * 1e6))
        sizes = sorted(size for _s, _e, size in gaps)
        if not any(size >= min_gap_ns for size in sizes):
            med = statistics.median(sizes) if sizes else 0
            min_gap_ns = int(max(0.3e6, med * 2))
            if sizes and not any(size >= min_gap_ns for size in sizes):
                min_gap_ns = int(0.3e6)
            degradations.append(
                f"没有空档达到 {max(0.3, min_step_ms):.3f}ms，回退到 {min_gap_ns / 1e6:.3f}ms"
            )
        period_items, period = period_signal(episodes, gaps, lo, hi, min_gap_ns)
        tol_ns = max(int(0.5e6), int(period * 0.06)) if period else int(0.5e6)
        idle_items = idle_signal(gaps, min_gap_ns)
        cand = {
            "idle": idle_items,
            "period": period_items,
            "host": host_signal(events, host_pid, lo, hi, min_gap_ns),
        }
        if not cand["period"]:
            degradations.append("忙闲序列自相关未找到稳定周期，周期维度不参与投票")
        if not cand["host"]:
            degradations.append("host 侧无明确空闲-恢复批次，host 维度不参与投票")
        sync_items = sync_signal(events, [it["t"] for it in cand["idle"]], tol_ns)
        if not cand["idle"] and not cand["host"]:
            sync_items = sync_fallback(events, lo, hi, min_gap_ns) or sync_items
            if sync_items:
                degradations.append("无设备空档与 host 批次，改用周期性 sync 兜底切分，置信度有限")
        cand["sync"] = sync_items
        boundaries = fuse(cand, lo, hi, tol_ns, int(min_step_ms * 1e6))
        notes.append(
            f"设备执行片段 {len(episodes)} 个，空档 {len(gaps)} 个，"
            f"边界阈值 {min_gap_ns / 1e6:.3f}ms"
        )
        if not boundaries:
            degradations.append("多信号均未给出边界，整窗输出并标记 uncertain")
        for b in boundaries:
            if b["confidence"] < min_confidence:
                b["uncertain"] = True

    steps = build_steps(boundaries, lo, hi, first_authority=force_authority)
    if not steps:
        steps = [{"step": 0, "start_ns": lo, "end_ns": hi, "confidence": 0.0,
                  "signals": [], "uncertain": True}]
        degradations.append("未切出 step，退化为整窗单步")

    conf = statistics.mean(b["confidence"] for b in boundaries) if boundaries else 0.0
    confidence = "high" if conf >= 0.75 and len(steps) >= 3 else ("medium" if conf >= 0.45 else "low")
    if not dev_tasks and not force_authority and confidence == "high":
        confidence = "medium"
    if boundaries and not force_authority:
        notes.append("首步起点没有前一边界，per_step[0].confidence 记 0 并标 uncertain")
    if any(s["uncertain"] for s in steps[1:]):
        notes.append("部分边界仅由单一信号支撑，已在 per_step 标 uncertain")

    check = None
    tol_ns = int(tolerance_ms * 1e6)
    if auth_starts and not skip_self_check and not force_authority:
        # 只剔除与窗口起点重合的权威标记；容差只用于后续匹配，不能连带删掉 1/2ms 的真实边界。
        truth = [a for a in auth_starts if a > steps[0]["start_ns"]]
        check = self_check([s["start_ns"] for s in steps[1:]], truth, tol_ns)
        notes.append(
            f"自检：与 {auth_source} 比对，F1={check['f1']}，"
            f"最大误差 {check['max_abs_err_ms']}ms"
        )
        if len(steps) > check["authority_count"] * 2:
            notes.append(
                "本次切分粒度比权威 step 细（常见于权威 step 只标记 profiler 窗口、"
                "而 trace 内含多个 decode 迭代）；需要粗粒度时调大 --min-step-ms"
            )
        if check["f1"] < 0.5:
            confidence = "low"
        elif check["f1"] < 0.9 and confidence == "high":
            confidence = "medium"

    steps_out = []
    for s in steps:
        steps_out.append(
            {
                "step": s["step"],
                "start_ms": round((s["start_ns"] - ui_zero) / 1e6, 3),
                "end_ms": round((s["end_ns"] - ui_zero) / 1e6, 3),
                "dur_ms": round((s["end_ns"] - s["start_ns"]) / 1e6, 3),
                "confidence": s["confidence"],
                "signals": s["signals"],
                "uncertain": s["uncertain"],
            }
        )

    metrics = [
        {
            "name": "step_count", "unit": "count", "base": len(steps), "cand": None,
            "delta": None, "delta_pct": None, "verdict": "info", "noise_band": None,
            "evidence": f"多信号融合（{', '.join(sorted(SIGNAL_WEIGHTS))}）",
        },
        {
            "name": "mean_step_ms", "unit": "ms",
            "base": round(statistics.mean(s["dur_ms"] for s in steps_out), 3),
            "cand": None, "delta": None, "delta_pct": None, "verdict": "info",
            "noise_band": None, "evidence": "per_step 时长均值",
        },
        {
            "name": "median_step_ms", "unit": "ms",
            "base": round(statistics.median(s["dur_ms"] for s in steps_out), 3),
            "cand": None, "delta": None, "delta_pct": None, "verdict": "info",
            "noise_band": None, "evidence": "per_step 时长中位数",
        },
        {
            "name": "mean_boundary_confidence", "unit": "ratio", "base": round(conf, 3),
            "cand": None, "delta": None, "delta_pct": None, "verdict": "info",
            "noise_band": None, "evidence": "各 step 起始边界置信度均值",
        },
    ]
    if check:
        metrics.append(
            {
                "name": "boundary_f1", "unit": "ratio", "base": check["f1"], "cand": None,
                "delta": None, "delta_pct": None,
                # F1 是切分质量指标，不是性能收益，统一用 info（A.1 里 regressed 表示性能退化）。
                "verdict": "info",
                "noise_band": None, "evidence": f"{auth_source} 自检",
            }
        )

    return {
        "skill": "kptools-ascend-profiling-step-split",
        "schema_version": 1,
        "mode": "single",
        "inputs": {"base": str(Path(trace_path).resolve()), "cand": None, "steps": "auto"},
        "data_quality": {"sidecars": sidecars, "degraded": degradations, "confidence": confidence},
        "metrics": metrics,
        "per_step": steps_out,
        "boundaries": [
            {
                "time_ms": round((b["t"] - ui_zero) / 1e6, 3),
                "confidence": b["confidence"],
                "signals": b["signals"],
                "evidence": b["evidence"],
                "uncertain": b["uncertain"],
            }
            for b in boundaries
        ],
        "self_check": check,
        "summary": f"切出 {len(steps)} 个 step，平均置信度 {conf:.2f}",
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 报告
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
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(result):
    steps = result["per_step"]
    total = steps[-1]["end_ms"] - steps[0]["start_ms"] if steps else 0
    bars = []
    colors = ["#6F8FA3", "#95879F", "#869B85", "#B08968", "#7A8FA6", "#A88C8C"]
    for i, s in enumerate(steps):
        left = (s["start_ms"] - steps[0]["start_ms"]) / total * 100 if total else 0
        width = s["dur_ms"] / total * 100 if total else 0
        color = colors[i % len(colors)]
        bars.append(
            f'<div class="fseg" style="left:{left:.3f}%;width:{width:.3f}%;background:{color}" '
            f'title="step {s["step"]}: {s["start_ms"]}~{s["end_ms"]} ms conf={s["confidence"]}">'
            f'<span class="lbl">#{s["step"]} {s["dur_ms"]:.1f}ms</span></div>'
        )
    step_rows = [
        [
            s["step"], f'{s["start_ms"]:.3f}', f'{s["end_ms"]:.3f}', f'{s["dur_ms"]:.3f}',
            s["confidence"], ", ".join(s["signals"]) or "-", "是" if s["uncertain"] else "否",
        ]
        for s in steps
    ]
    b_rows = [
        [
            b["time_ms"], b["confidence"], ", ".join(b["signals"]) or "-",
            esc("; ".join(f"{k}: {v}" for k, v in b["evidence"].items())),
            "是" if b["uncertain"] else "否",
        ]
        for b in result["boundaries"]
    ]
    check = result["self_check"]
    check_html = "<p>未做权威自检（无 STEP_TIME / ProfilerStep / kernel_details Step Id）。</p>"
    if check:
        check_html = _table(
            ["权威边界", "本次边界", "匹配", "precision", "recall", "F1", "容差(ms)", "最大误差(ms)"],
            [[check["authority_count"], check["our_count"], check["matched"], check["precision"],
              check["recall"], check["f1"], check["tolerance_ms"], check["max_abs_err_ms"]]],
        )
    metric_rows = [
        [m["name"], m["base"], m["unit"], esc(m["evidence"])] for m in result["metrics"]
    ]
    notes = "".join(f"<li>{esc(n)}</li>" for n in result["notes"]) or "<li>无</li>"
    degraded = "".join(f"<li>{esc(d)}</li>" for d in result["data_quality"]["degraded"]) or "<li>无</li>"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Step 切分报告</title><style>{_css()}</style></head><body>
<div class="page on">
<h1>无标识 trace 深度 Step 切分</h1>
<div class="sect">
<p class="sem">输入：{esc(result["inputs"]["base"])}</p>
<p class="sem">总体置信度：<b>{esc(result["data_quality"]["confidence"])}</b>；{esc(result["summary"])}</p>
<div class="flame" style="height:22px">{"".join(bars)}</div>
</div>
<h2 class="s-P1">判定指标</h2>
<div class="sect">{_table(["指标", "值", "单位", "依据"], metric_rows)}</div>
<h2 class="s-P2">Step 列表</h2>
<div class="sect">{_table(["step", "start(ms)", "end(ms)", "dur(ms)", "置信度", "信号", "uncertain"], step_rows)}</div>
<h2 class="s-P2">边界证据</h2>
<div class="sect">{_table(["time(ms)", "置信度", "信号", "依据", "uncertain"], b_rows)}</div>
<h2 class="s-P3">权威自检</h2>
<div class="sect">{check_html}</div>
<h2 class="s-P3">降级项</h2><div class="sect"><ul>{degraded}</ul></div>
<h2 class="s-P3">口径与限制</h2><div class="sect"><ul>{notes}</ul>
<p class="sem">切分基于设备忙闲、sync、host 批次与周期一致性；不等价于真实 Python 调用栈边界。信号冲突时以 uncertain 标出，不强行给结论。</p></div>
</div></body></html>"""


def _write_error_html(path, trace, exc):
    Path(path).write_text(
        f"<!doctype html><meta charset='utf-8'><h1>Step 切分失败</h1>"
        f"<p>输入: {esc(trace)}</p><p>错误: {esc(exc)}</p>",
        encoding="utf-8",
    )


_INPUT_NAMES = ("trace_view.json", "analysis.db", "kernel_details.csv",
                "step_trace_time.csv", "communication.json")


def _input_files(trace):
    p = Path(trace).expanduser()
    if p.is_file():
        return {p.resolve()}
    out = set()
    # 目录形态：实际只会读取 locate_trace 解析到的 trace，但同目录侧车也属于
    # 采集现场，一并纳入拒绝集合，避免输出覆盖原始数据。
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file() and (f.name in _INPUT_NAMES
                                or f.name.startswith("ascend_pytorch_profiler_")):
                out.add(f.resolve())
    return out


def _check_outputs(trace, html_path, json_path):
    """拒绝输出覆盖输入采集（含目录形态的实际 trace/侧车）、JSON/HTML 同名、符号链接。"""
    try:
        real_trace = locate_trace(trace).resolve()
    except Exception as exc:
        raise ValueError(f"无法解析输入 trace，拒绝写入输出: {exc}")
    inputs = _input_files(trace)
    inputs.add(real_trace)
    outs = {}
    if html_path:
        outs["--out-html"] = Path(html_path).expanduser()
    if json_path:
        outs["--json"] = Path(json_path).expanduser()
    if html_path and json_path and Path(html_path).expanduser().resolve() == Path(json_path).expanduser().resolve():
        raise ValueError("--out-html 与 --json 不能指向同一文件")
    for flag, p in outs.items():
        if p.is_symlink():
            raise ValueError(f"{flag} {p} 是符号链接，拒绝写入")
        if p.resolve() in inputs:
            raise ValueError(f"{flag} {p} 指向输入采集文件，拒绝覆盖")


def main(argv=None):
    ap = argparse.ArgumentParser(description="无标识 trace 的深度 step 切分")
    ap.add_argument("trace", help="trace_view.json")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--out-html", dest="html_path")
    ap.add_argument("--min-step-ms", type=float, default=0.5)
    ap.add_argument("--min-confidence", type=float, default=0.5)
    ap.add_argument("--tolerance-ms", type=float, default=2.0, help="自检匹配容差")
    ap.add_argument("--authority", action="store_true", help="强制使用权威边界（STEP_TIME/ProfilerStep）")
    ap.add_argument("--no-self-check", action="store_true")
    args = ap.parse_args(argv)

    try:
        _check_outputs(args.trace, args.html_path, args.json_path)
    except ValueError as exc:
        print(f"输出路径不安全，拒绝写入：{exc}", file=sys.stderr)
        return 3

    try:
        result = analyze(
            args.trace,
            min_step_ms=args.min_step_ms,
            min_confidence=args.min_confidence,
            tolerance_ms=args.tolerance_ms,
            force_authority=args.authority,
            skip_self_check=args.no_self_check,
        )
    except Exception as exc:  # 始终产出可读结果
        if args.html_path:
            _write_error_html(args.html_path, args.trace, exc)
        print(f"错误: {exc}", file=sys.stderr)
        return 3

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.html_path:
        Path(args.html_path).write_text(render_html(result), encoding="utf-8")

    print(f"steps: {len(result['per_step'])}  置信度: {result['data_quality']['confidence']}")
    if result["self_check"]:
        c = result["self_check"]
        print(f"自检: F1={c['f1']} precision={c['precision']} recall={c['recall']}")
    for s in result["per_step"]:
        print(f"  #{s['step']} {s['start_ms']:.3f}~{s['end_ms']:.3f} ms "
              f"dur={s['dur_ms']:.3f} conf={s['confidence']} signals={','.join(s['signals']) or '-'}")
    for d in result["data_quality"]["degraded"]:
        print(f"  degrade: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
