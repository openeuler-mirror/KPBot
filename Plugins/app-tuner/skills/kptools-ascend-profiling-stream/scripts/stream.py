#!/usr/bin/env python3
"""kptools-ascend-profiling-stream：NPU stream 结构画像。

只消费 trace_view.json 的设备事件（stream 以 args.streamId/stream_id 优先，其次 tid），
输出每流忙碌占比、时间加权并发度、逐 step 并发度与 Event Record→Wait 跨流依赖配对。
单份输入 mode=single（只画像），两份输入 mode=pair（Δ + MAD 噪声带）。

区间/统计工具向 kptools-ascend-profiling-compare/scripts/kpc_core.py 复制并裁剪
（_union/_cover/_median/_mad/_round），不 import 任何其他 skill 的脚本。
"""

import argparse
import bisect
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

SKILL_NAME = "kptools-ascend-profiling-stream"
SCHEMA_VERSION = 1
HERE = Path(__file__).resolve().parent
CSS_PATH = HERE.parent / "assets" / "report.css"
AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
US_TO_NS = 1000.0
STRIP_RE = re.compile(r"[^a-z0-9]")


# ---------------------------------------------------------------------------
# 通用工具（复制自 kpc_core.py，按需裁剪）
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
    vals = [float(x) for x in xs if x is not None]
    if not vals:
        return 0.0
    m = _median(vals)
    return _median([abs(x - m) for x in vals])


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


# ---------------------------------------------------------------------------
# trace 读取
# ---------------------------------------------------------------------------
def locate_trace(path):
    """返回 trace_view.json 路径；找不到抛 FileNotFoundError。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    if p.is_file():
        if p.name == "trace_view.json":
            return p
        candidate = p.parent / "trace_view.json"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"给定文件不是 trace_view.json 且同目录无 trace: {p}")
    for candidate in (
        p / "trace_view.json",
        p / "ASCEND_PROFILER_OUTPUT" / "trace_view.json",
    ):
        if candidate.is_file():
            return candidate
    hits = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if hits:
        return hits[0]
    raise FileNotFoundError(f"目录下未找到 trace_view.json: {p}")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_key(key):
    return STRIP_RE.sub("", str(key).lower())


def load_events(path):
    """读取并归一化 X 事件；ts/dur 统一换算为 ns（trace 输入单位按 µs 处理）。"""
    trace = locate_trace(path)
    try:
        raw = json.loads(trace.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise ValueError(f"trace_view.json 解析失败: {exc}") from exc
    events = raw.get("traceEvents") if isinstance(raw, dict) else raw
    if not isinstance(events, list):
        raise ValueError("trace_view.json 结构异常：未找到事件数组")
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = _num(e.get("ts"))
        dur = _num(e.get("dur"))
        if ts is None or dur is None or dur <= 0:
            continue
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        out.append(
            {
                "name": str(e.get("name") or ""),
                "cat": str(e.get("cat") or ""),
                "pid": e.get("pid"),
                "tid": e.get("tid"),
                "ts": ts * US_TO_NS,
                "dur": dur * US_TO_NS,
                "args": args,
            }
        )
    return trace, out


def _task_type(ev):
    """真实 CANN trace 把任务类型放在 args["Task Type"]（cat 为 None）。"""
    for key, value in (ev.get("args") or {}).items():
        if _norm_key(key) == "tasktype":
            return str(value)
    return ""


_STREAM_KEYS = ("physicstreamid", "streamid", "stream", "logicalstreamid")


def _stream_from_args(ev):
    """按固定优先级取设备 stream 字段，避免仅因 JSON 字段顺序不同而改变分组。

    先取 Physic Stream Id（物理流），再回退到逻辑流标识（streamId / stream / logicalStreamId）。
    """
    args = {}
    for key, value in (ev.get("args") or {}).items():
        args.setdefault(_norm_key(key), value)
    for key in _STREAM_KEYS:
        value = args.get(key)
        if isinstance(value, (int, float, str)) and str(value) != "":
            return str(value)
    return None


def _stream_of(ev):
    """stream 标识：Physic Stream Id / streamId 优先，其次 tid。"""
    sid = _stream_from_args(ev)
    if sid is not None:
        return sid
    if ev.get("tid") is not None:
        return str(ev["tid"])
    return None


def _dep_stream_of(ev):
    """跨流依赖只认设备 stream 字段；host 事件的 tid 不是 stream，不能回退。"""
    return _stream_from_args(ev)


def _event_id(ev):
    # 优先专用 id 字段；同一事件若同时带 id / notify_id，不能让通用 id 抢先。
    args = {_norm_key(k): v for k, v in (ev.get("args") or {}).items()}
    for key in ("notifyid", "eventid", "eventhandle", "id"):
        value = args.get(key)
        if isinstance(value, (int, float, str)) and str(value) != "":
            return str(value)
    return None


def _dep_role(ev):
    text = f'{ev["name"]} {ev["cat"]}'.lower().replace("_", "").replace(" ", "")
    if "notifyrecord" in text or "eventrecord" in text:
        return "record"
    if "notifywait" in text or "eventwait" in text:
        return "wait"
    if "wait" in text and ("notify" in text or "event" in text):
        return "wait"
    if "record" in text and ("notify" in text or "event" in text):
        return "record"
    return None


def _is_device_kernel(ev):
    """只看计算/通信 kernel；拷贝/DMA（含 SDMA_SQE/PCIE_DMA_SQE 描述符）归 h2d-d2h。"""
    if ev["name"].lower().startswith("hcom"):
        return True
    tt = _task_type(ev)
    if tt:
        return tt in AI_TASK_TYPES
    return ev["cat"] in AI_TASK_TYPES


def _steps_from_trace(events):
    """保留真实 ProfilerStep#N 编号，不重编号（重编号会让不同侧被错配成同一步）。"""
    steps = []
    for ev in events:
        if not ev["name"].startswith("ProfilerStep"):
            continue
        try:
            sid = int(ev["name"].split("#", 1)[1])
        except (IndexError, ValueError):
            sid = None
        steps.append((sid, ev["ts"], ev["ts"] + ev["dur"]))
    steps.sort(key=lambda x: (x[1], x[0] if x[0] is not None else -1))
    if any(sid is None for sid, _s, _e in steps):
        steps = [
            (sid if sid is not None else i, s, e)
            for i, (sid, s, e) in enumerate(steps)
        ]
    return steps


def _steps_from_db(trace_path):
    """从 ascend_pytorch_profiler_*.db:STEP_TIME 取绝对 step 边界。"""
    roots = [trace_path.parent] + list(trace_path.parents)[1:3]
    for root in roots:
        for db in sorted(root.glob("ascend_pytorch_profiler_*.db")):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                rows = con.execute(
                    "SELECT id,startNs,endNs FROM STEP_TIME ORDER BY startNs"
                ).fetchall()
                con.close()
                steps = [(int(i), int(s), int(e)) for i, s, e in rows if int(e) > int(s)]
            except Exception:  # noqa: BLE001 - 侧车不可读/字段异常则继续找
                continue
            if steps:
                return steps, f"{db.name}:STEP_TIME"
    return [], None


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------
def _concurrency_curve(kernels, w0, w1):
    """扫描线：返回 [(t0,t1,并发数)]、时间加权平均值、峰值。"""
    points = []
    for k in kernels:
        s, e = max(k["start"], w0), min(k["end"], w1)
        if e > s:
            points.append((s, 1))
            points.append((e, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    segments, cur, prev, area, peak = [], 0, w0, 0.0, 0
    i = 0
    while i < len(points):
        t = points[i][0]
        if t > prev:
            segments.append((prev, t, cur))
            area += cur * (t - prev)
        while i < len(points) and points[i][0] == t:
            cur += points[i][1]
            i += 1
        peak = max(peak, cur)
        prev = t
    if prev < w1:
        segments.append((prev, w1, cur))
        area += cur * (w1 - prev)
    merged = []
    for seg in segments:
        if merged and merged[-1][2] == seg[2] and merged[-1][1] == seg[0]:
            merged[-1] = (merged[-1][0], seg[1], seg[2])
        else:
            merged.append(seg)
    span = w1 - w0
    return merged, (area / span if span > 0 else 0.0), peak


def _avg_in_window(segments, a, b):
    if b <= a:
        return 0.0
    area = sum(max(0.0, min(t1, b) - max(t0, a)) * c for t0, t1, c in segments)
    return area / (b - a)


def _empty_analysis(path, reason):
    return {
        "path": str(Path(path).expanduser().resolve()) if Path(path).exists() else str(path),
        "trace": None,
        "events": [],
        "kernels": [],
        "streams": [],
        "window": (0.0, 0.0),
        "segments": [],
        "steps": [],
        "deps": {"pairs": [], "pairing_rate": None, "records": 0, "waits": 0},
        "sidecars": [],
        "degraded": [reason],
        "notes": [],
        "missing_stream": False,
        "no_steps": True,
        "empty": True,
        "metrics": {
            "stream_count": 0,
            "stream_busy_pct": 0.0,
            "stream_busy_max_pct": 0.0,
            "stream_concurrency_avg": 0.0,
            "stream_concurrency_max": 0,
            "stream_concurrency_per_step": 0.0,
            "stream_cross_dep_count": 0,
        },
        "confidence": "low",
    }


def analyze_capture(path):
    trace, events = load_events(path)
    degraded, notes, sidecars = [], [], []
    kernels = [
        {
            "name": ev["name"],
            "cat": ev["cat"],
            "stream": _stream_of(ev),
            "start": ev["ts"],
            "end": ev["ts"] + ev["dur"],
        }
        for ev in events
        if _is_device_kernel(ev)
    ]
    if not kernels:
        res = _empty_analysis(path, "未发现设备 kernel（cat∈AI_* / hcom）")
        res["trace"] = str(trace)
        res["events"] = events
        return res

    missing_stream = all(k["stream"] is None for k in kernels)
    if missing_stream:
        for k in kernels:
            k["stream"] = "all"
        degraded.append("无法区分 stream（设备事件无 tid/args.streamId），全部归为单流")
    elif any(k["stream"] is None for k in kernels):
        notes.append("部分设备事件无法区分 stream，已归为 unknown")
    for k in kernels:
        if k["stream"] is None:
            k["stream"] = "unknown"

    w0 = min(k["start"] for k in kernels)
    w1 = max(k["end"] for k in kernels)
    # 有 ProfilerStep 时，忙碌占比按「采集步窗口」而整个 trace 时间轴计算，
    # 否则预热/收尾的空闲会把所有流的忙碌占比稀释。
    span_steps = _steps_from_trace(events)
    if span_steps:
        w0 = span_steps[0][1]
        w1 = max(e for _, s, e in span_steps)

    grouped = {}
    for k in kernels:
        grouped.setdefault(k["stream"], []).append((k["start"], k["end"]))
    streams = []
    for sid, ivs in grouped.items():
        busy = _cover(ivs, w0, w1)
        streams.append(
            {
                "stream": sid,
                "kernel_count": sum(1 for k in kernels if k["stream"] == sid),
                "busy_ns": busy,
                "busy_pct": (busy / (w1 - w0) * 100.0) if w1 > w0 else 0.0,
            }
        )
    streams.sort(key=lambda s: (-s["busy_pct"], s["stream"]))

    segments, conc_avg, conc_max = _concurrency_curve(kernels, w0, w1)

    steps = _steps_from_trace(events)
    if not steps:
        steps, sidecar = _steps_from_db(trace)
        if sidecar:
            sidecars.append(sidecar)
    no_steps = not steps
    if no_steps:
        steps = [(0, w0, w1)]
        degraded.append("未发现 step 边界（ProfilerStep#N / STEP_TIME），per_step 使用整窗")
    step_rows = [
        {
            "step": sid,
            "start": s,
            "end": e,
            "concurrency_avg": _avg_in_window(segments, s, e),
        }
        for sid, s, e in steps
    ]

    dep_events = [ev for ev in events if _dep_role(ev)]
    skipped_no_stream = 0
    records = []
    waits = []
    for ev in dep_events:
        role = _dep_role(ev)
        eid = _event_id(ev)
        if eid is None:
            continue
        sid = _dep_stream_of(ev)
        if sid is None:
            skipped_no_stream += 1
            continue
        (records if role == "record" else waits).append((eid, sid, ev["ts"]))
    if skipped_no_stream:
        notes.append(
            f"{skipped_no_stream} 个 Event Record/Wait 事件没有设备 stream 字段，"
            "未参与跨流依赖配对（host tid 不作为 stream）"
        )
    dep_pairs = []
    paired = 0
    # 预建 event id → 按时间排序的 Record 索引，避免每个 Wait 扫描整份列表（N²）。
    record_index = {}
    for eid, sid, ts in records:
        record_index.setdefault(eid, []).append((ts, sid))
    for lst in record_index.values():
        lst.sort()
    record_ts = {eid: [ts for ts, _sid in lst] for eid, lst in record_index.items()}
    has_id = any(eid is not None for eid, _s, _t in waits) or any(
        _event_id(ev) for ev in dep_events
    )
    if dep_events and not has_id:
        degraded.append("Event Record/Wait 事件缺少 event id 字段，跨流依赖无法配对")
        pairing_rate = None
    elif dep_events:
        waits_with_id = [(eid, sid, ts) for eid, sid, ts in waits if eid is not None]
        for eid, sid, ts in waits_with_id:
            lst = record_index.get(eid)
            if not lst:
                continue
            pos = bisect.bisect_right(record_ts[eid], ts) - 1
            rec_ts, rec_sid = lst[pos] if pos >= 0 else lst[0]
            paired += 1
            if rec_sid != sid:
                dep_pairs.append(
                    {
                        "event_id": eid,
                        "record_stream": rec_sid,
                        "wait_stream": sid,
                        "record_ts": rec_ts,
                        "wait_ts": ts,
                    }
                )
        pairing_rate = (paired / len(waits_with_id) * 100.0) if waits_with_id else None
    else:
        pairing_rate = None

    if dep_events:
        if not waits:
            notes.append("存在 Event Record 事件但没有 Wait 事件，跨流依赖计数为 0")
        elif pairing_rate is None:
            notes.append("跨流依赖配对率: 不可计算（缺 event id）")
        else:
            notes.append(
                f"事件配对率: {paired}/{len(waits)} Wait（{pairing_rate:.1f}%）；"
                f"其中跨流依赖 {len(dep_pairs)} 对"
            )
            if pairing_rate < 50.0:
                notes.append(
                    f"跨流依赖配对率偏低（{pairing_rate:.1f}%），依赖数与拓扑结论需打折"
                )
    else:
        notes.append("未发现 Event Record/Wait 事件，跨流依赖计数为 0")

    busy_pcts = [s["busy_pct"] for s in streams]
    step_avgs = [s["concurrency_avg"] for s in step_rows]
    metrics = {
        "stream_count": len(streams),
        "stream_busy_pct": _median(busy_pcts),
        "stream_busy_max_pct": max(busy_pcts) if busy_pcts else 0.0,
        "stream_concurrency_avg": conc_avg,
        "stream_concurrency_max": conc_max,
        "stream_concurrency_per_step": _median(step_avgs),
        "stream_cross_dep_count": len(dep_pairs),
    }

    notes.insert(0, "trace ts/dur 按 µs 解释并统一换算为 ns")
    notes.append("忙碌为单流区间并集 / 采集窗口；设备级空闲窗口归 kptools-ascend-profiling-compare-bubble")

    confidence = "low" if (missing_stream or no_steps) else "medium"
    return {
        "path": str(Path(path).expanduser().resolve()),
        "trace": str(trace),
        "events": events,
        "kernels": kernels,
        "streams": streams,
        "window": (w0, w1),
        "segments": segments,
        "steps": step_rows,
        "deps": {
            "pairs": dep_pairs,
            "pairing_rate": pairing_rate,
            "records": len(records),
            "waits": len(waits),
        },
        "sidecars": sidecars,
        "degraded": degraded,
        "notes": notes,
        "missing_stream": missing_stream,
        "no_steps": no_steps,
        "empty": False,
        "metrics": metrics,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 对比与 A.1 信封
# ---------------------------------------------------------------------------
METRIC_DEFS = (
    ("stream_count", "count", "流数量"),
    ("stream_busy_pct", "pct", "每流忙碌占比中位数"),
    ("stream_busy_max_pct", "pct", "最忙流忙碌占比"),
    ("stream_concurrency_avg", "count", "时间加权平均并发度"),
    ("stream_concurrency_max", "count", "峰值并发度"),
    ("stream_concurrency_per_step", "count", "每步并发度中位数"),
    ("stream_cross_dep_count", "count", "跨流依赖数"),
)

EVIDENCE = {
    "stream_count": "trace_view.json: 设备事件 stream 去重计数（args.streamId 优先，其次 tid）",
    "stream_busy_pct": "trace_view.json: 每流 kernel 区间并集 / 采集窗口",
    "stream_busy_max_pct": "trace_view.json: 每流 kernel 区间并集 / 采集窗口",
    "stream_concurrency_avg": "trace_view.json: kernel 端点扫描线，面积/窗口（时间加权）",
    "stream_concurrency_max": "trace_view.json: kernel 端点扫描线峰值",
    "stream_concurrency_per_step": "trace_view.json: ProfilerStep#N（或 STEP_TIME）区间内扫描线",
    "stream_cross_dep_count": "trace_view.json: Event Record → Wait 按 event id 配对",
}


def _noise_series(key, cap):
    if key in ("stream_busy_pct", "stream_busy_max_pct"):
        return [s["busy_pct"] for s in cap["streams"]]
    if key in ("stream_concurrency_avg", "stream_concurrency_max",
               "stream_concurrency_per_step"):
        return [s["concurrency_avg"] for s in cap["steps"]]
    return []


def _confidence_of(base, cand):
    if cand is None:
        return base["confidence"]
    order = {"low": 0, "medium": 1, "high": 2}
    worst = min((base["confidence"], cand["confidence"]), key=lambda c: order[c])
    return worst


def build_envelope(base, cand, min_delta_ms):
    mode = "pair" if cand is not None else "single"
    metrics = []
    for key, unit, _label in METRIC_DEFS:
        b = base["metrics"][key]
        if cand is None:
            metrics.append(
                {
                    "name": key,
                    "unit": unit,
                    "base": _round(b),
                    "cand": None,
                    "delta": None,
                    "delta_pct": None,
                    "verdict": "info",
                    "noise_band": 0.0,
                    "evidence": EVIDENCE[key],
                }
            )
            continue
        c = cand["metrics"][key]
        delta = b - c
        noise = _mad(_noise_series(key, base)) + _mad(_noise_series(key, cand))
        verdict = "noise" if abs(delta) <= noise else "info"
        pct = (delta / b * 100.0) if b else None
        metrics.append(
            {
                "name": key,
                "unit": unit,
                "base": _round(b),
                "cand": _round(c),
                "delta": _round(delta),
                "delta_pct": _round(pct) if pct is not None else None,
                "verdict": verdict,
                "noise_band": _round(noise),
                "evidence": EVIDENCE[key],
            }
        )

    per_step = []
    base_by_id = {s["step"]: s for s in base["steps"]}
    cand_by_id = {s["step"]: s for s in cand["steps"]} if cand else {}
    for sid in sorted(set(base_by_id) | set(cand_by_id)):
        b_row = base_by_id.get(sid)
        c_row = cand_by_id.get(sid)
        row = {
            "step": sid,
            "base": _round(b_row["concurrency_avg"]) if b_row else None,
        }
        if cand:
            row["cand_step"] = sid if c_row else None
            row["cand"] = _round(c_row["concurrency_avg"]) if c_row else None
            row["delta"] = (
                _round(row["base"] - row["cand"])
                if (b_row is not None and c_row is not None)
                else None
            )
        per_step.append(row)

    degraded = [f"base: {x}" for x in base["degraded"]]
    if cand is not None:
        degraded += [f"cand: {x}" for x in cand["degraded"]]
    sidecars = list(base["sidecars"])
    if cand is not None:
        sidecars += [x for x in cand["sidecars"] if x not in sidecars]

    if cand is not None:
        notes = [f"base: {x}" for x in base["notes"]]
        notes += [f"cand: {x}" for x in cand["notes"]]
    else:
        notes = list(base["notes"])
    if cand is not None:
        if {s["step"] for s in base["steps"]} != {s["step"] for s in cand["steps"]}:
            notes.append(
                "两份采集 step id 不一致"
                f"（base={sorted(s['step'] for s in base['steps'])}, "
                f"cand={sorted(s['step'] for s in cand['steps'])}），"
                "per_step 按共同 step id 对齐，缺失侧为 null"
            )
        notes.append("Δ = base − cand；stream 指标是描述性的，超出噪声带记 info，不做改善/恶化判定")
        notes.append("噪声带 = MAD(base 分布) + MAD(cand 分布)；--min-delta-ms 对 pct/count 指标不适用")

    if cand is not None:
        summary = (
            f"流 {base['metrics']['stream_count']}→{cand['metrics']['stream_count']} 条；"
            f"忙碌中位数 {base['metrics']['stream_busy_pct']:.1f}%→"
            f"{cand['metrics']['stream_busy_pct']:.1f}%；"
            f"平均并发 {base['metrics']['stream_concurrency_avg']:.2f}→"
            f"{cand['metrics']['stream_concurrency_avg']:.2f}；"
            f"跨流依赖 {base['metrics']['stream_cross_dep_count']}→"
            f"{cand['metrics']['stream_cross_dep_count']}"
        )
    elif base["empty"]:
        summary = "未发现设备 kernel"
    else:
        summary = (
            f"单份画像：流 {base['metrics']['stream_count']} 条；"
            f"忙碌中位数 {base['metrics']['stream_busy_pct']:.1f}%；"
            f"平均并发 {base['metrics']['stream_concurrency_avg']:.2f}"
            f"（峰值 {base['metrics']['stream_concurrency_max']}）；"
            f"跨流依赖 {base['metrics']['stream_cross_dep_count']} 条"
        )

    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "inputs": {
            "base": base["path"],
            "cand": cand["path"] if cand else None,
            "steps": f"base={len(base['steps'])}"
            + (f", cand={len(cand['steps'])}" if cand else ""),
        },
        "data_quality": {
            "sidecars": sidecars,
            "degraded": degraded,
            "confidence": _confidence_of(base, cand),
        },
        "metrics": metrics,
        "per_step": per_step,
        "summary": summary,
        "notes": notes,
        "_base": base,
        "_cand": cand,
    }


def public_envelope(env):
    return {k: v for k, v in env.items() if not k.startswith("_")}


def _topology(base, cand):
    bmap = {s["stream"]: s["busy_pct"] for s in base["streams"]}
    cmap = {s["stream"]: s["busy_pct"] for s in cand["streams"]}
    rows = []
    for sid in sorted(set(bmap) | set(cmap), key=str):
        b = bmap.get(sid)
        c = cmap.get(sid)
        if b is None:
            status = "新增"
        elif c is None:
            status = "消失"
        else:
            status = "共有"
        rows.append(
            {
                "stream": sid,
                "base": b,
                "cand": c,
                "delta": (b - c) if (b is not None and c is not None) else None,
                "status": status,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(value):
    return html.escape(str(value), quote=True)


def _css():
    return CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""


EXTRA_CSS = """
.tl{width:100%;height:auto;display:block;background:#FAF8F5;border:1px solid #E8E4DC;border-radius:6px}
.tl text{font-size:10px;fill:#7B858D}
.tl .axis{stroke:#D8D3C8;stroke-width:1}
.tl .stepv{stroke:#B08968;stroke-width:1;stroke-dasharray:3 3}
.tl .line{fill:none;stroke:#4A78A0;stroke-width:1.6}
.tl .area{fill:#4A78A0;opacity:.14;stroke:none}
"""


def _ms(ns):
    return ns / 1e6


def _svg_timeline(analysis, height=150):
    w0, w1 = analysis["window"]
    segments = analysis["segments"]
    if not segments or w1 <= w0:
        return '<div class="note">无并发度时间线（未发现设备 kernel）。</div>'
    width, pad_l, pad_r, pad_t, pad_b = 980, 42, 14, 12, 26
    maxc = max((c for _t0, _t1, c in segments), default=1) or 1
    span = w1 - w0

    def x(t):
        return pad_l + (t - w0) / span * (width - pad_l - pad_r)

    def y(c):
        return height - pad_b - (c / maxc) * (height - pad_t - pad_b)

    pts = []
    for t0, t1, c in segments:
        pts.append(f"{x(t0):.1f},{y(c):.1f}")
        pts.append(f"{x(t1):.1f},{y(c):.1f}")
    poly = " ".join(pts)
    area = (
        f"{x(w0):.1f},{y(0):.1f} " + poly + f" {x(w1):.1f},{y(0):.1f}"
    )
    step_lines = "".join(
        f'<line class="stepv" x1="{x(s["start"]):.1f}" y1="{pad_t}" '
        f'x2="{x(s["start"]):.1f}" y2="{height - pad_b}"/>'
        for s in analysis["steps"]
        if w0 < s["start"] < w1
    )
    return (
        '<svg class="tl" viewBox="0 0 980 %d" role="img">' % height
        + f'<line class="axis" x1="{pad_l}" y1="{y(0):.1f}" '
        f'x2="{width - pad_r}" y2="{y(0):.1f}"/>'
        + f'<line class="axis" x1="{pad_l}" y1="{pad_t}" '
        f'x2="{pad_l}" y2="{y(0):.1f}"/>'
        + f'<text x="4" y="{y(maxc) + 4:.1f}">{maxc}</text>'
        + f'<text x="4" y="{y(0) + 4:.1f}">0</text>'
        + step_lines
        + f'<polygon class="area" points="{area}"/>'
        + f'<polyline class="line" points="{poly}"/>'
        + f'<text x="{pad_l}" y="{height - 6}">0 ms</text>'
        + f'<text x="{width - pad_r - 54}" y="{height - 6}">{_ms(span):.2f} ms</text>'
        + "</svg>"
    )


def _table(headers, rows):
    out = ["<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _fmt(value, digits=2, suffix=""):
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return esc(value)


def _stream_table(analysis):
    rows = []
    for s in analysis["streams"]:
        rows.append(
            [
                esc(s["stream"]),
                str(s["kernel_count"]),
                _fmt(_ms(s["busy_ns"]), 3, " ms"),
                _fmt(s["busy_pct"], 2, "%"),
            ]
        )
    return _table(["stream", "kernel 数", "忙碌时长", "忙碌占比"], rows)


def _deps_section(analysis):
    deps = analysis["deps"]
    if deps["pairs"]:
        rows = []
        for p in deps["pairs"][:200]:
            rows.append(
                [
                    esc(p["event_id"]),
                    esc(p["record_stream"]),
                    esc(p["wait_stream"]),
                    _fmt(_ms(p["wait_ts"]), 3, " ms"),
                ]
            )
        body = _table(["event id", "record stream", "wait stream", "wait 时间"], rows)
        if len(deps["pairs"]) > 200:
            body += f'<div class="note">仅显示前 200 条，共 {len(deps["pairs"])} 条。</div>'
        return body
    if deps["records"] or deps["waits"]:
        rate = deps["pairing_rate"]
        extra = f"配对率 {rate:.1f}%" if rate is not None else "缺 event id，无法配对"
        return f'<div class="note">发现 Event 事件但未形成跨流依赖（{esc(extra)}）。</div>'
    return '<div class="note">未发现 Event Record/Wait 事件，跨流依赖计数为 0。</div>'


def _metrics_section(env):
    rows = []
    for m in env["metrics"]:
        label = dict((k, lbl) for k, _u, lbl in METRIC_DEFS)[m["name"]]
        if env["mode"] == "pair":
            rows.append(
                [
                    esc(label),
                    _fmt(m["base"]),
                    _fmt(m["cand"]),
                    _fmt(m["delta"]),
                    _fmt(m["noise_band"]),
                    esc(m["verdict"]),
                ]
            )
        else:
            rows.append([esc(label), _fmt(m["base"]), esc(m["unit"])])
    if env["mode"] == "pair":
        return _table(["指标", "base", "cand", "Δ", "噪声带", "判定"], rows)
    return _table(["指标", "值", "单位"], rows)


def _step_table(env):
    rows = []
    for p in env["per_step"]:
        if env["mode"] == "pair":
            rows.append(
                [str(p["step"]), _fmt(p.get("base")), _fmt(p.get("cand")),
                 _fmt(p.get("delta"))]
            )
        else:
            rows.append([str(p["step"]), _fmt(p.get("base"))])
    if not rows:
        return '<div class="note">无 step 数据。</div>'
    if env["mode"] == "pair":
        return _table(["step", "base 并发度", "cand 并发度", "Δ"], rows)
    return _table(["step", "平均并发度"], rows)


def _topology_section(env):
    rows = []
    for t in _topology(env["_base"], env["_cand"]):
        rows.append(
            [
                esc(t["stream"]),
                _fmt(t["base"], 2, "%") if t["base"] is not None else "—",
                _fmt(t["cand"], 2, "%") if t["cand"] is not None else "—",
                _fmt(t["delta"], 2, "%") if t["delta"] is not None else "—",
                esc(t["status"]),
            ]
        )
    return _table(["stream", "base 忙碌占比", "cand 忙碌占比", "Δ", "状态"], rows)


def _capture_block(label, analysis):
    parts = [
        f'<div class="kv">{esc(label)}: {esc(analysis["path"])} · 来源 trace_view.json</div>',
        f'<div class="kv">窗口 {_ms(analysis["window"][1] - analysis["window"][0]):.3f} ms · '
        f'流 {len(analysis["streams"])} 条 · kernel {len(analysis["kernels"])} 个 · '
        f'置信度 {esc(analysis["confidence"])}</div>',
        '<h3>并发度时间线（时间加权）</h3>',
        _svg_timeline(analysis),
        '<div class="barleg">蓝线为同时在跑的 kernel 数；棕色虚线为 step 边界。</div>',
        '<h3>每流忙碌占比</h3>',
        _stream_table(analysis),
        '<h3>逐 step 平均并发度</h3>',
        _step_table(_single_env(analysis)),
        '<h3>跨流依赖清单</h3>',
        _deps_section(analysis),
    ]
    return '<div class="sect">' + "".join(parts) + "</div>"


def _single_env(analysis):
    rows = [
        {"step": s["step"], "base": _round(s["concurrency_avg"])}
        for s in analysis["steps"]
    ]
    return {"mode": "single", "per_step": rows}


def render_html(env):
    base = env["_base"]
    cand = env["_cand"]
    quality = env["data_quality"]
    hero = (
        '<div class="hero">'
        f'<div class="htitle">stream 画像 · {"双份对比" if env["mode"] == "pair" else "单份分析"}'
        f'<span class="judge"> {esc(env["summary"])}</span></div>'
        f'<div class="hnums">流 <b>{base["metrics"]["stream_count"]}</b>'
        f'<span class="sep">·</span>忙碌中位数 <b>{base["metrics"]["stream_busy_pct"]:.1f}%</b>'
        f'<span class="sep">·</span>平均并发 <b>{base["metrics"]["stream_concurrency_avg"]:.2f}</b>'
        f'<span class="sep">·</span>峰值 {base["metrics"]["stream_concurrency_max"]}'
        f'<span class="sep">·</span>跨流依赖 {base["metrics"]["stream_cross_dep_count"]}</div>'
        f'<div class="barleg">模式 {esc(env["mode"])} · 置信度 {esc(quality["confidence"])} · '
        f'侧车 {esc(", ".join(quality["sidecars"]) or "无")}</div>'
        "</div>"
    )
    body = [_capture_block("base", base)]
    if cand is not None:
        body.append(_capture_block("cand", cand))
        body.append(
            '<div class="sect"><h2 class="s-P2">流拓扑变化</h2>'
            + _topology_section(env)
            + "</div>"
        )
    body.append(
        '<div class="sect"><h2 class="s-P1">指标'
        + ("对比" if cand is not None else "画像")
        + "</h2>"
        + _metrics_section(env)
        + ("<h3>逐 step 并发度对比</h3>" + _step_table(env) if cand is not None else "")
        + "</div>"
    )
    degrade = "".join(f"<li>{esc(x)}</li>" for x in quality["degraded"])
    notes = "".join(f"<li>{esc(x)}</li>" for x in env["notes"])
    body.append(
        '<div class="sect"><h2 class="s-P3">降级说明与口径</h2>'
        + (f"<h3>降级项</h3><ul>{degrade}</ul>" if degrade else '<div class="note">无降级。</div>')
        + f"<h3>口径说明</h3><ul>{notes}</ul>"
        + "</div>"
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>NPU stream 画像</title><style>"
        + _css()
        + EXTRA_CSS
        + "</style></head><body><div class=\"page on\">"
        + "<h1>NPU stream 结构分析（每流忙碌 / 并发度 / 跨流依赖）</h1>"
        + hero
        + "".join(body)
        + "</div></body></html>"
    )


def render_error(err):
    msg = esc(err)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>stream 分析失败</title><style>"
        + _css()
        + '</style></head><body><div class="page on"><h1>stream 分析失败</h1>'
        '<div class="sect"><div class="note">输入不可读或无法解析。</div>'
        f"<pre>{msg}</pre></div></div></body></html>"
    )


def error_envelope(base, cand, err):
    return {
        "skill": SKILL_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "pair" if cand else "single",
        "inputs": {"base": str(base), "cand": str(cand) if cand else None,
                   "steps": "不可读"},
        "data_quality": {
            "sidecars": [],
            "degraded": [f"输入不可读: {err}"],
            "confidence": "low",
        },
        "metrics": [
            {"name": key, "unit": unit, "base": 0, "cand": 0, "delta": 0,
             "delta_pct": None, "verdict": "info", "noise_band": 0.0,
             "evidence": EVIDENCE[key]}
            for key, unit, _label in METRIC_DEFS
        ],
        "per_step": [],
        "summary": f"分析失败: {err}",
        "notes": ["输入不可读时仍输出 HTML 错误页与 JSON，退出码 3"],
    }


def validate(env):
    """A.1 信封自检（规则照抄 SKILL_DEV_PLAN 附录 A.1）。"""
    errs = []
    for key in ("skill", "schema_version", "mode", "inputs", "data_quality",
                "metrics", "per_step", "summary", "notes"):
        if key not in env:
            errs.append(f"缺字段 {key}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 必须是 pair | single")
    dq = env.get("data_quality") or {}
    for key in ("sidecars", "degraded", "confidence"):
        if key not in dq:
            errs.append(f"data_quality 缺字段 {key}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 必须是 high | medium | low")
    if not env.get("metrics"):
        errs.append("metrics 不能为空")
    for i, m in enumerate(env.get("metrics") or []):
        for key in ("name", "unit", "base", "cand", "delta", "delta_pct",
                    "verdict", "noise_band", "evidence"):
            if key not in m:
                errs.append(f"metrics[{i}] 缺字段 {key}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 必须是数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 必须是数组")
    return errs


def run(base_path, cand_path=None, min_delta_ms=0.05):
    base = analyze_capture(base_path)
    cand = analyze_capture(cand_path) if cand_path else None
    env = build_envelope(base, cand, min_delta_ms)
    errs = validate(public_envelope(env))
    if errs:
        env["notes"].extend(f"A.1 自检: {e}" for e in errs)
    return env


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="NPU stream 结构分析：每流忙碌占比 / 并发度 / 跨流依赖"
    )
    ap.add_argument("base", help="采集目录或 trace_view.json")
    ap.add_argument("cand", nargs="?", help="第二份采集（可选，给出即双份对比）")
    ap.add_argument("--out-html", default="stream_report.html")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--min-delta-ms", type=float, default=0.05,
                    help="兼容 CLI 约定；stream 指标为 pct/count，不套用毫秒下限")
    args = ap.parse_args(argv)

    try:
        env = run(args.base, args.cand, args.min_delta_ms)
        html_text = render_html(env)
        rc = 0
    except Exception as exc:  # noqa: BLE001 - 保证一定产出错误页
        env = error_envelope(args.base, args.cand, exc)
        html_text = render_error(exc)
        rc = 3

    out_html = Path(args.out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html_text, encoding="utf-8")
    if args.json_out:
        out_json = Path(args.json_out)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(public_envelope(env), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON: {out_json}")
    print(env["summary"])
    print(f"HTML: {out_html}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
