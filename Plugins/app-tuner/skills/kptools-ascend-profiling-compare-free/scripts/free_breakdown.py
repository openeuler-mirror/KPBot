#!/usr/bin/env python3
"""Free 时间细化分类（free）：把设备空闲拆成可行动的子类。

子类优先级（高→低）：host_busy → sync_wait → dispatch_gap → comm_align → memcpy_gap → unknown。
输出 A.1 信封 + HTML（每步构成 + Top 窗口 + unknown 占比告警）。

用法:
  free_breakdown.py <base> [<cand>] [--out-html r.html] [--json r.json]
                    [--min-gap-ms 0.01] [--adjacent-ms 1.0] [--min-delta-ms 0.05]
"""

import argparse
import bisect
import json
import re
import sys
from pathlib import Path

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
COPY_TASK_TYPES = {"SDMA_SQE", "PCIE_DMA_SQE"}
SCHEMA_VERSION = 1
CLASSES = ("host_busy", "sync_wait", "dispatch_gap", "comm_align", "memcpy_gap", "unknown")
UNKNOWN_WARN_PCT = 30.0
MEMCPY_HINT = ("memcpy", "memset", "transdata", "h2d", "d2h", "d2d")


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


def union(ivs):
    out = []
    for a, b in sorted((float(s), float(e)) for s, e in ivs if e > s):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def cover(ivs, a, b):
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in union(ivs))


class IntervalIndex:
    """预合并区间 + 起点索引：每个窗口的覆盖率查询 O(log n + k)，不重复排序/合并。"""

    def __init__(self, ivs):
        self.ivs = union(ivs)
        self.starts = [s for s, _ in self.ivs]

    def cover(self, a, b):
        total = 0.0
        i = max(0, bisect.bisect_right(self.starts, a) - 1)
        while i < len(self.ivs):
            s, e = self.ivs[i]
            if s >= b:
                break
            total += max(0.0, min(e, b) - max(s, a))
            i += 1
        return total

    def any_overlap(self, a, b):
        return self.cover(a, b) > 0


def _has_point(sorted_points, a, b):
    i = bisect.bisect_left(sorted_points, a)
    return i < len(sorted_points) and sorted_points[i] <= b


def _near_flags(positions, flags, lo, hi):
    i = bisect.bisect_left(positions, lo)
    j = bisect.bisect_right(positions, hi)
    return {flags[k] for k in range(i, j)}


def complement(ivs, a, b):
    gaps, prev = [], a
    for s, e in union(ivs):
        if s > prev:
            gaps.append((prev, min(s, b)))
        prev = max(prev, e)
        if prev >= b:
            break
    if prev < b:
        gaps.append((prev, b))
    return gaps


def locate(path):
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"输入不存在: {p}")
    if p.is_file():
        return (p.parent, p) if p.name == "trace_view.json" else (p.parent, p.parent / "trace_view.json")
    direct = p / "ASCEND_PROFILER_OUTPUT"
    if direct.is_dir():
        return direct, direct / "trace_view.json"
    traces = sorted(p.rglob("trace_view.json"), key=lambda x: len(x.parts))
    if traces:
        return traces[0].parent, traces[0]
    return p, p / "trace_view.json"


def _cat_low(e):
    return str(e.get("cat") or "").lower()


def _task_type(e):
    """真实 CANN trace：任务类型在 args["Task Type"]，cat 为 None。"""
    a = e.get("args")
    if isinstance(a, dict):
        for k, v in a.items():
            if re.sub(r"[^a-z]", "", str(k).lower()) == "tasktype":
                return str(v)
    return ""


def _is_memcpy(name):
    low = str(name).lower()
    return any(k in low for k in MEMCPY_HINT)


def load_capture(path, notes, degradations):
    output_dir, trace_path = locate(path)
    if not trace_path.exists():
        raise FileNotFoundError(f"未找到 trace_view.json: {trace_path}")
    try:
        events = json.loads(trace_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise FileNotFoundError(f"trace_view.json 解析失败: {exc}")
    if not isinstance(events, list):
        raise FileNotFoundError("trace_view.json 不是事件数组")

    spans = []
    for e in events:
        if isinstance(e, dict) and e.get("ph") == "X" \
                and str(e.get("name") or "").startswith("ProfilerStep#"):
            try:
                ts, dur = float(e.get("ts")), float(e.get("dur") or 0)
            except (TypeError, ValueError):
                continue
            if dur > 0:
                try:
                    sid = int(str(e.get("name")).split("#", 1)[1])
                except (IndexError, ValueError):
                    sid = len(spans)
                spans.append((sid, ts, ts + dur))
    spans.sort(key=lambda x: x[1])
    if not spans:
        degradations.append("未找到 ProfilerStep#N，按可观测 host/设备事件整窗处理（无 per-step 构成）")

    kernels, host_compute, syncs, enqueue_ends, memcpys = [], [], [], [], []
    for e in events:
        if not isinstance(e, dict) or e.get("ph") != "X":
            continue
        try:
            ts, dur = float(e.get("ts")), float(e.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue
        name = str(e.get("name") or "").strip()
        cat = _cat_low(e)
        iv = (ts, ts + dur)
        tt = _task_type(e)
        cat_is_comm = cat == "communication"
        is_comm = name.startswith("hcom") or "hccl" in tt.lower() or cat_is_comm
        is_dev = (
            tt in AI_TASK_TYPES
            or tt in COPY_TASK_TYPES
            or cat_is_comm
            or (not tt and (
                cat in {c.lower() for c in AI_TASK_TYPES}
                or cat in {c.lower() for c in COPY_TASK_TYPES}
                or name.startswith("hcom")))
        )
        if is_dev:
            kernels.append({"iv": iv, "name": name[:120], "comm": is_comm})
        elif "synchronize" in name.lower():
            syncs.append(iv)
        elif cat == "enqueue":
            enqueue_ends.append(ts + dur)
        elif not name.startswith("ProfilerStep#") and (
                cat in ("python_function", "cpu_op", "operator") or "aten::" in name):
            host_compute.append(iv)
        if _is_memcpy(name):
            memcpys.append(iv)
    if not kernels:
        raise FileNotFoundError("trace 内未识别到设备 kernel，无法计算空闲")
    notes.append(f"kernel {len(kernels)}、host_compute {len(host_compute)}、sync {len(syncs)}、"
                 f"enqueue 结束点 {len(enqueue_ends)}、memcpy {len(memcpys)}")
    return {"path": str(trace_path), "source": "trace_view.json", "spans": spans,
            "kernels": kernels, "host_compute": host_compute, "syncs": syncs,
            "enqueue_ends": enqueue_ends, "memcpys": memcpys}


def _prepare(cap):
    """预合并 host/sync 区间并建立时间索引，避免每个空闲窗口重复排序/合并整份采集。"""
    ctx = dict(cap)
    ctx["host_index"] = IntervalIndex(cap["host_compute"])
    ctx["sync_index"] = IntervalIndex(cap["syncs"])
    ctx["enqueue_sorted"] = sorted(cap["enqueue_ends"])
    k_starts, k_start_flags, k_ends, k_end_flags = [], [], [], []
    for k in cap["kernels"]:
        s, e = k["iv"]
        flag = "comm" if k["comm"] else "kernel"
        k_starts.append(s)
        k_start_flags.append(flag)
        k_ends.append(e)
        k_end_flags.append(flag)
    order = sorted(range(len(k_starts)), key=lambda i: k_starts[i])
    ctx["k_start_pos"] = [k_starts[i] for i in order]
    ctx["k_start_flag"] = [k_start_flags[i] for i in order]
    order = sorted(range(len(k_ends)), key=lambda i: k_ends[i])
    ctx["k_end_pos"] = [k_ends[i] for i in order]
    ctx["k_end_flag"] = [k_end_flags[i] for i in order]
    ctx["m_start"] = sorted(s for s, _e in cap["memcpys"])
    ctx["m_end"] = sorted(e for _s, e in cap["memcpys"])
    return ctx


def _adjacent(win, ctx, tol):
    a, b = win
    kinds = set()
    kinds |= _near_flags(ctx["k_start_pos"], ctx["k_start_flag"], b - tol, b + tol)
    kinds |= _near_flags(ctx["k_end_pos"], ctx["k_end_flag"], a - tol, a + tol)
    if _has_point(ctx["m_start"], b - tol, b + tol) or \
            _has_point(ctx["m_end"], a - tol, a + tol):
        kinds.add("memcpy")
    return kinds


def classify(win, ctx, tol):
    a, b = win
    length = b - a
    if length <= 0:
        return None
    if ctx["host_index"].cover(a, b) / length >= 0.5 and \
            not _has_point(ctx["enqueue_sorted"], a, b):
        return "host_busy"
    if ctx["sync_index"].any_overlap(a, b):
        return "sync_wait"
    if _has_point(ctx["enqueue_sorted"], a, b):
        return "dispatch_gap"
    kinds = _adjacent(win, ctx, tol)
    if "comm" in kinds:
        return "comm_align"
    if "memcpy" in kinds:
        return "memcpy_gap"
    return "unknown"


def _observation_window(cap):
    """无 ProfilerStep 时的整窗：覆盖可观测的 host/设备/同步/memcpy 事件，而非仅首末 kernel。"""
    starts, ends = [], []
    for k in cap["kernels"]:
        s, e = k["iv"]
        starts.append(s)
        ends.append(e)
    for group in (cap["host_compute"], cap["syncs"], cap["memcpys"]):
        for s, e in group:
            starts.append(s)
            ends.append(e)
    starts.extend(cap["enqueue_ends"])
    ends.extend(cap["enqueue_ends"])
    if not starts:
        return 0.0, 0.0
    return min(starts), max(ends)


def analyse(cap, min_gap_us, adjacent_ms):
    tol = adjacent_ms * 1000.0
    ctx = _prepare(cap)
    per_step = {}
    windows = []
    busy_all = [(k["iv"]) for k in cap["kernels"]]
    if cap["spans"]:
        steps = [(sid, a, b) for sid, a, b in cap["spans"]]
        whole = False
    else:
        lo, hi = _observation_window(cap)
        if hi <= lo:
            lo = min((k["iv"][0] for k in cap["kernels"]), default=0.0)
            hi = max((k["iv"][1] for k in cap["kernels"]), default=lo)
        steps = [(0, lo, hi)]
        whole = True
    for sid, a, b in steps:
        step_busy = [iv for iv in busy_all if iv[1] > a and iv[0] < b]
        agg = {c: 0.0 for c in CLASSES}
        for g in complement(step_busy, a, b):
            if (g[1] - g[0]) < min_gap_us:
                continue
            cls = classify(g, ctx, tol)
            if cls is None:
                continue
            agg[cls] += (g[1] - g[0]) / 1000.0
            windows.append({"step": sid, "start_us": round4(g[0], 1), "end_us": round4(g[1], 1),
                            "dur_ms": round4((g[1] - g[0]) / 1000.0), "class": cls})
        agg["total"] = sum(agg[c] for c in CLASSES)
        per_step[sid] = agg
    return {"per_step": per_step, "windows": windows, "whole": whole}


def totals(analysis):
    agg = {c: 0.0 for c in CLASSES}
    for row in analysis["per_step"].values():
        for c in CLASSES:
            agg[c] += row[c]
    agg["total"] = sum(agg[c] for c in CLASSES)
    agg["unknown_pct"] = (agg["unknown"] / agg["total"] * 100.0) if agg["total"] > 0 else 0.0
    return agg


def metric(name, unit, base, cand, noise, info=False, single=False):
    if single:
        return {"name": name, "unit": unit, "base": round4(base), "cand": None,
                "delta": None, "delta_pct": None, "verdict": "info",
                "noise_band": 0.0, "evidence": "trace_view.json"}
    delta = base - cand
    verdict = "info" if info else (
        "improved" if delta > noise else ("regressed" if delta < -noise else "noise"))
    return {"name": name, "unit": unit, "base": round4(base), "cand": round4(cand),
            "delta": round4(delta),
            "delta_pct": round4((delta / base * 100.0) if base else 0.0, 2),
            "verdict": verdict, "noise_band": round4(noise), "evidence": "trace_view.json"}


METRIC_MAP = (
    ("free_total_ms", "total"),
    ("free_host_busy_ms", "host_busy"),
    ("free_sync_wait_ms", "sync_wait"),
    ("free_dispatch_gap_ms", "dispatch_gap"),
    ("free_comm_align_ms", "comm_align"),
    ("free_memcpy_gap_ms", "memcpy_gap"),
    ("free_unknown_ms", "unknown"),
)


def compare(base, cand, min_delta_ms, min_gap_ms=0.01, adjacent_ms=1.0):
    single = cand is None
    b_an = analyse(base, min_gap_ms * 1000.0, adjacent_ms)
    c_an = analyse(cand, min_gap_ms * 1000.0, adjacent_ms) if cand else None
    bt = totals(b_an)
    ct = totals(c_an) if c_an else bt
    b_series = [row["total"] for row in b_an["per_step"].values()]
    if single:
        noise = 0.0
    else:
        c_series = [row["total"] for row in c_an["per_step"].values()]
        noise = max(min_delta_ms, mad(b_series) + mad(c_series))

    metrics = []
    for name, key in METRIC_MAP:
        info = key in ("host_busy", "sync_wait", "dispatch_gap", "comm_align", "memcpy_gap", "unknown")
        metrics.append(metric(name, "ms", bt[key], ct[key],
                              0.0 if info else noise, info=info, single=single))
    metrics.append(metric("free_unknown_pct", "pct", bt["unknown_pct"], ct["unknown_pct"],
                          0.0, info=True, single=single))

    if single:
        summary = f"Free {bt['total']:.3f}ms；unknown 占比 {bt['unknown_pct']:.1f}%"
    else:
        summary = (f"Free {bt['total']:.3f} → {ct['total']:.3f}ms；"
                   f"unknown 占比 {bt['unknown_pct']:.1f}% → {ct['unknown_pct']:.1f}%")
    def _comp(row, prefix):
        return {f"{prefix}_{c}": round4(row.get(c, 0.0)) for c in CLASSES}

    b_ids = list(b_an["per_step"])
    per_step = []
    step_count_note = None
    if single:
        for bs in b_ids:
            row = b_an["per_step"][bs]
            item = {"step": bs, "base": round4(row["total"]), "cand": None, "delta": None}
            item.update(_comp(row, "base"))
            per_step.append(item)
    else:
        c_ids = list(c_an["per_step"])
        common = [s for s in b_ids if s in c_an["per_step"]]
        if len(b_ids) != len(c_ids):
            step_count_note = (
                f"base/cand step 数不一致（{len(b_ids)} vs {len(c_ids)}），"
                "仅按共同 step id 比较；free_total_ms 不做方向判定"
            )
            for m in metrics:
                if m["name"] == "free_total_ms":
                    m["verdict"] = "info"
        for bs in common:
            brow, crow = b_an["per_step"][bs], c_an["per_step"][bs]
            item = {"step": bs, "cand_step": bs,
                    "base": round4(brow["total"]), "cand": round4(crow["total"]),
                    "delta": round4(brow["total"] - crow["total"])}
            item.update(_comp(brow, "base"))
            item.update({f"cand_{c}": round4(crow.get(c, 0.0)) for c in CLASSES})
            per_step.append(item)
    return {"metrics": metrics, "per_step": per_step, "summary": summary,
            "step_count_note": step_count_note,
            "base_an": b_an, "cand_an": c_an, "base_totals": bt,
            "cand_totals": ct if c_an else bt}


def envelope(base, cand, result, notes, degradations):
    degr = [f"base: {x}" for x in degradations[0]]
    if cand:
        degr += [f"cand: {x}" for x in degradations[1]]
    if result.get("step_count_note"):
        degr.append(result["step_count_note"])
    if result["base_totals"]["unknown_pct"] > UNKNOWN_WARN_PCT:
        degr.append(f"base: unknown 占比 {result['base_totals']['unknown_pct']:.1f}% > {UNKNOWN_WARN_PCT}%，归因不充分")
    if cand and result["cand_totals"]["unknown_pct"] > UNKNOWN_WARN_PCT:
        degr.append(f"cand: unknown 占比 {result['cand_totals']['unknown_pct']:.1f}% > {UNKNOWN_WARN_PCT}%，归因不充分")
    conf = "low" if any("未找到 ProfilerStep" in x for x in degr) else ("medium" if degr else "high")
    note_list = [x for pair in notes for x in pair] + [
        "子类优先级：host_busy > sync_wait > dispatch_gap > comm_align > memcpy_gap > unknown",
        "host_busy 要求 host 计算事件覆盖窗口 ≥50% 且窗口内无下发完成",
        "free_total_ms 只统计 ≥ --min-gap-ms 的空闲窗口；子类时长为描述性指标（info）",
    ]
    if cand:
        note_list.append("Δ = base − cand，正值为候选更省")
    return {
        "skill": "kptools-ascend-profiling-compare-free",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair" if cand else "single",
        "inputs": {"base": base["path"], "cand": cand["path"] if cand else None,
                   "steps": f"per_step={len(result['per_step'])}"},
        "data_quality": {"sidecars": ["trace_view.json"], "degraded": degr, "confidence": conf},
        "metrics": result["metrics"],
        "per_step": result["per_step"],
        "summary": result["summary"],
        "notes": note_list,
    }


def validate(env):
    """A.1 信封自检，口径与 compare/scripts/envelope.py 的 validate 一致。"""
    errs = []
    for k in ("skill", "schema_version", "mode", "inputs", "data_quality",
              "metrics", "per_step", "summary", "notes"):
        if k not in env:
            errs.append(f"缺 {k}")
    if env.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if env.get("mode") not in ("pair", "single"):
        errs.append("mode 非法")
    dq = env.get("data_quality") or {}
    for k in ("sidecars", "degraded", "confidence"):
        if k not in dq:
            errs.append(f"data_quality 缺 {k}")
    if dq.get("confidence") not in ("high", "medium", "low"):
        errs.append("confidence 非法")
    if not isinstance(dq.get("sidecars"), list):
        errs.append("sidecars 非数组")
    if not isinstance(dq.get("degraded"), list):
        errs.append("degraded 非数组")
    metrics = env.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errs.append("metrics 必须是非空数组")
        metrics = metrics if isinstance(metrics, list) else []
    for i, m in enumerate(metrics):
        if not isinstance(m, dict):
            errs.append(f"metrics[{i}] 非对象")
            continue
        for k in ("name", "unit", "base", "cand", "delta", "delta_pct",
                  "verdict", "noise_band", "evidence"):
            if k not in m:
                errs.append(f"metrics[{i}] 缺 {k}")
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
        if not m.get("evidence"):
            errs.append(f"metrics[{i}].evidence 不能为空")
    if not isinstance(env.get("per_step"), list):
        errs.append("per_step 非数组")
    if not isinstance(env.get("summary"), str) or not env.get("summary"):
        errs.append("summary 必须是非空字符串")
    if not isinstance(env.get("notes"), list):
        errs.append("notes 非数组")
    return errs


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cls(v):
    return {"improved": "v-dev", "regressed": "v-host"}.get(v, "v-mid")


def render(env, result, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    rows = "".join(
        f"<tr><td>{esc(m['name'])}</td><td>{m['base']}</td><td>{m['cand']}</td>"
        f"<td>{'—' if m['delta'] is None else format(m['delta'], '+.3f')}</td>"
        f"<td><span class='verdict {_cls(m['verdict'])}'>"
        f"{esc(m['verdict'])}</span></td></tr>" for m in env["metrics"])
    table = ("<div class='sect'><h3>Free 构成对比</h3><table><tr><th>指标</th><th>base</th>"
             "<th>cand</th><th>Δ</th><th>判定</th></tr>" + rows + "</table></div>")
    wins = sorted(result["base_an"]["windows"], key=lambda w: -w["dur_ms"])[:10]
    top = "".join(f"<tr><td>step{w['step']}</td><td>{w['start_us']}</td><td>{w['end_us']}</td>"
                  f"<td>{w['dur_ms']:.3f}</td><td>{esc(w['class'])}</td></tr>" for w in wins)
    top_html = ("<div class='sect'><h3>base 最长空闲窗口（便于回 trace 定位）</h3><table>"
                "<tr><th>step</th><th>start(µs)</th><th>end(µs)</th><th>时长(ms)</th><th>归因</th></tr>"
                f"{top}</table></div>" if top else "")
    deg = ("<div class='sect'><h3>降级与口径</h3><ul>"
           + "".join(f"<li>{esc(x)}</li>" for x in env["data_quality"]["degraded"] + env["notes"])
           + "</ul></div>")
    hero = ("<div class='hero'>"
            f"<div class='htitle'>Free 细化 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='kv'>契约置信度: {esc(env['data_quality']['confidence'])}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>Free 细化</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>Free 时间细化分类</h1>"
            f"{hero}{table}{top_html}{deg}</div></body></html>")


def render_error(msg, base, cand, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>Free 分析失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>Free 分析失败</h1>"
            f"<div class='hero'><div class='htitle'>无法完成分析</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base)}</div><div class='kv'>cand: {esc(cand) if cand else '（单份）'}</div>"
            "</div></body></html>")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Free 时间细化分类（单份或双份对比）")
    ap.add_argument("base")
    ap.add_argument("cand", nargs="?", default=None)
    ap.add_argument("--out-html", default="free_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    ap.add_argument("--min-gap-ms", type=float, default=0.01)
    ap.add_argument("--adjacent-ms", type=float, default=1.0)
    args = ap.parse_args(argv)

    css_path = Path(__file__).resolve().parent.parent / "assets" / "report.css"
    nb, nc, db, dc = [], [], [], []
    try:
        base = load_capture(args.base, nb, db)
        cand = load_capture(args.cand, nc, dc) if args.cand else None
        result = compare(base, cand, args.min_delta_ms, args.min_gap_ms, args.adjacent_ms)
    except Exception as exc:
        Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_html).write_text(render_error(f"{type(exc).__name__}: {exc}",
                                                    args.base, args.cand, css_path), encoding="utf-8")
        print(f"分析失败: {exc}", file=sys.stderr)
        return 3

    env = envelope(base, cand, result, [nb, nc], [db, dc])
    errs = validate(env)
    if errs:
        print("警告：JSON 不符合 A.1: " + "; ".join(errs), file=sys.stderr)
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(env, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"JSON → {args.out_json}")
    Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_html).write_text(render(env, result, css_path), encoding="utf-8")
    print(f"HTML → {args.out_html}")
    print(f"结论: {env['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
