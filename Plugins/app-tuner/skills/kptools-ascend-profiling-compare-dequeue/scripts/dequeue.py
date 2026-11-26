#!/usr/bin/env python3
"""Host Dequeue（排队等待）时延统计：下发完成→设备启动的等待分布、每步等待总量、等待占比。

输出 A.1 信封 + HTML。数据源为 trace_view.json（host API 事件 + 设备 kernel 事件），
按「host 下发完成 → 同窗口内首个设备 kernel 启动」配对；本 skill 只关注其中的**等待**部分。

用法:
  dequeue.py <base> <cand> [--out-html r.html] [--json r.json]
             [--pair-window-ms 5.0] [--min-delta-ms 0.05]
"""

import argparse
import bisect
import json
import re
import sys
from pathlib import Path

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
# 只保留有实测出处、且确实是拷贝本体的类型；WRITE_VALUE_SQE/MEM_WRITE_VALUE 无来源，已移除。
COPY_KERNEL_TYPES = {"SDMA_SQE", "PCIE_DMA_SQE"}
HOST_CATS = {"pytorch_api", "cann_api", "cann", "operator", "enqueue"}
SCHEMA_VERSION = 1
# 占比类指标的最小噪声带（百分点），避免任何微小差异都判 improved/regressed。
SHARE_NOISE_FLOOR_PCT = 0.5


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


def pctl(xs, p):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(pos), min(len(xs) - 1, int(pos) + 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


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


def cover_all(ivs):
    return sum(e - s for s, e in union(ivs))


def intersect_len(a_ivs, b_ivs):
    """两组区间的交集总长（双指针，O(n+m)）。"""
    A, B = union(a_ivs), union(b_ivs)
    i = j = 0
    total = 0.0
    while i < len(A) and j < len(B):
        a0, a1 = A[i]
        b0, b1 = B[j]
        total += max(0.0, min(a1, b1) - max(a0, b0))
        if a1 < b1:
            i += 1
        else:
            j += 1
    return total


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


NON_ENQUEUE_HINTS = (
    "sync", "query", "malloc", "free", "memcpy", "record", "wait", "stream",
    "event", "get", "set", "attr",
)


def _is_host_api(name, cat):
    low = str(name).lower()
    return (str(cat).lower() in HOST_CATS or low.startswith("ascendcl@")
            or "aclnn" in low or low.startswith("enqueue@"))


def _task_type(e):
    a = e.get("args")
    if isinstance(a, dict):
        for k, v in a.items():
            if re.sub(r"[^a-z]", "", str(k).lower()) == "tasktype":
                return str(v)
    return ""


def _corr(e):
    a = e.get("args")
    if not isinstance(a, dict):
        return None
    for k, v in a.items():
        if re.sub(r"[^a-z]", "", str(k).lower()) == "correlationid":
            return str(v)
    return None


def load_capture(path, notes, degradations):
    output_dir, trace_path = locate(path)
    if not trace_path.exists():
        raise FileNotFoundError(f"未找到 trace_view.json: {trace_path}")
    size = trace_path.stat().st_size
    if size > 512 * 1024 * 1024:
        raise FileNotFoundError(
            f"trace_view.json 过大（{size / 1e6:.0f}MB > 512MB），拒绝整文件加载以避免 OOM"
        )
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise FileNotFoundError(f"trace_view.json 解析失败: {exc}")
    if isinstance(data, dict):
        data = data.get("traceEvents") or data.get("events") or []
    if not isinstance(data, list):
        raise FileNotFoundError("trace_view.json 不是事件数组")
    events = data

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
        degradations.append("未找到 ProfilerStep#N，按整窗统计")

    hosts, kernels, dequeues = [], [], []
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
        cat = str(e.get("cat") or "")
        step = 0
        for sid, a, b in spans:
            if a <= ts < b:
                step = sid
                break
        if cat == "dequeue":
            dequeues.append({"start": ts, "end": ts + dur, "dur": dur,
                             "name": name.split("@", 1)[-1][:120], "corr": _corr(e), "step": step})
        else:
            tt = _task_type(e)
            is_dev = (tt in AI_TASK_TYPES or tt in COPY_KERNEL_TYPES) if tt else (
                cat in AI_TASK_TYPES or name.startswith("hcom"))
            if is_dev:
                kernels.append({"start": ts, "end": ts + dur, "name": name[:120], "step": step})
            elif _is_host_api(name, cat):
                hosts.append({"start": ts, "end": ts + dur, "dur": dur,
                              "name": name.split("@", 1)[-1][:120], "cat": cat,
                              "corr": _corr(e), "step": step})
    enq_only = [h for h in hosts if h.get("cat") == "enqueue"]
    if enq_only:
        hosts = enq_only
    if not hosts and not kernels:
        raise FileNotFoundError("trace 内既无 host API 事件也无设备 kernel")
    if not hosts:
        degradations.append("未识别到 host API 事件，无法统计下发耗时")
    if not kernels:
        degradations.append("未识别到设备 kernel，无法配对下发→启动延迟")
    notes.append(f"host API {len(hosts)} 条、设备 kernel {len(kernels)} 条")
    if dequeues:
        notes.append("排队等待来自 trace 的 cat=dequeue 与 correlation_id 精确配对")
    return {"path": str(trace_path), "source": "trace_view.json", "hosts": hosts,
            "kernels": kernels, "dequeues": dequeues,
            "steps": sorted({h["step"] for h in hosts}) or [0],
            "window": (min((h["start"] for h in hosts), default=0.0),
                       max((h["end"] for h in hosts), default=0.0))}


def pair_enqueues(cap, window_ms):
    """优先用 correlation_id 把 enqueue 与 dequeue 精确配对；否则退回 kernel 启发式。

    返回 (pairs, paired, mode)，mode ∈ {"exact", "heuristic"}：
    - exact: wait = dequeue.start − enqueue.end（host 队列等待）
    - heuristic: wait = kernel.start − host.end（设备启动延迟）
    """
    deqs = cap.get("dequeues") or []
    if deqs:
        by_corr = {}
        for q in deqs:
            if q.get("corr") is not None:
                by_corr.setdefault(q["corr"], []).append(q)
        pairs, used = [], 0
        for h in sorted(cap["hosts"], key=lambda x: x["start"]):
            cid = h.get("corr")
            bucket = by_corr.get(cid) if cid is not None else None
            if not bucket:
                continue
            bucket.sort(key=lambda q: q["start"])
            q = bucket[0]
            if q["start"] >= h["end"] - 1e-6:
                bucket.pop(0)
                pairs.append({"host": h, "kernel": q,
                              "latency_us": max(0.0, q["start"] - h["end"])})
                used += 1
            elif q["start"] < h["end"] - 1e-6:
                # 时间不合法（dequeue 早于 enqueue）保留在桶里，避免静默丢弃。
                continue
        if pairs:
            return pairs, used, "exact"
    window = window_ms * 1000.0
    ks = sorted(enumerate(cap["kernels"]), key=lambda kv: kv[1]["start"])
    starts = [kern["start"] for _idx, kern in ks]
    used = set()
    pairs = []
    for h in sorted(cap["hosts"], key=lambda x: (x["end"], x["start"])):
        i = bisect.bisect_left(starts, h["end"] - 1e-6)
        while i < len(ks):
            idx, kern = ks[i]
            if kern["start"] > h["end"] + window:
                break
            if idx not in used:
                used.add(idx)
                pairs.append({"host": h, "kernel": kern,
                              "latency_us": max(0.0, kern["start"] - h["end"])})
                break
            i += 1
    return pairs, len(used), "heuristic"


def stats(cap, pairs, paired):
    hosts = cap["hosts"]
    host_busy = cover_all([(h["start"], h["end"]) for h in hosts])
    kernel_busy = cover_all([(k["start"], k["end"]) for k in cap["kernels"]])
    overlap = intersect_len([(h["start"], h["end"]) for h in hosts],
                            [(k["start"], k["end"]) for k in cap["kernels"]])
    steps = max(1, len(cap["steps"]))
    lats = [p["latency_us"] / 1000.0 for p in pairs]
    per_step = {}
    for s in cap["steps"]:
        rows = [p for p in pairs if p["host"]["step"] == s]
        per_step[s] = sum(p["latency_us"] for p in rows) / 1000.0
    wait_total = sum(lats)
    share = (wait_total / (wait_total + kernel_busy / 1000.0) * 100.0) if (wait_total + kernel_busy) > 0 else 0.0
    return {
        "dequeue_p50_ms": pctl(lats, 50), "dequeue_p95_ms": pctl(lats, 95),
        "dequeue_total_per_step_ms": wait_total / steps,
        "dequeue_queue_wait_share_pct": share,
        "pair_rate_pct": (paired / len(hosts) * 100.0) if hosts else 0.0,
        "overlap_pct": (overlap / host_busy * 100.0) if host_busy > 0 else 0.0,
        "host_busy_us": host_busy, "kernel_busy_us": kernel_busy,
        "per_step": per_step, "host_count": len(hosts), "lats_ms": lats,
        "pair_count": len(pairs),
        "top_latency": sorted(pairs, key=lambda p: -p["latency_us"])[:10],
    }


def metric(name, unit, base, cand, noise, info=False, invert=False):
    delta = base - cand
    if info:
        verdict = "info"
    elif invert:
        verdict = "improved" if delta < -noise else ("regressed" if delta > noise else "noise")
    else:
        verdict = "improved" if delta > noise else ("regressed" if delta < -noise else "noise")
    return {"name": name, "unit": unit, "base": round4(base), "cand": round4(cand),
            "delta": round4(delta),
            "delta_pct": round4((delta / base * 100.0) if base else 0.0, 2),
            "verdict": verdict, "noise_band": round4(noise), "evidence": "trace_view.json"}


def compare(base, cand, min_delta_ms, window_ms=5.0):
    b_pairs, b_paired, b_mode = pair_enqueues(base, window_ms)
    c_pairs, c_paired, c_mode = pair_enqueues(cand, window_ms)
    bst = stats(base, b_pairs, b_paired)
    cst = stats(cand, c_pairs, c_paired)
    common = [s for s in base["steps"] if s in cst["per_step"]]
    b_series = [bst["per_step"][s] for s in common]
    c_series = [cst["per_step"][s] for s in common]
    noise = max(min_delta_ms, mad(b_series) + mad(c_series))
    # 同一口径也用于 duration/latency 系列

    mode_mismatch = b_mode != c_mode
    no_latency = not bst["lats_ms"] or not cst["lats_ms"]
    latency_info = mode_mismatch or no_latency
    no_kernels = not base["kernels"] and not cand["kernels"]
    lat_noise = max(min_delta_ms, mad(bst["lats_ms"]) + mad(cst["lats_ms"]))
    metrics = [
        metric("dequeue_p50_ms", "ms", bst["dequeue_p50_ms"], cst["dequeue_p50_ms"],
               lat_noise, info=latency_info),
        metric("dequeue_p95_ms", "ms", bst["dequeue_p95_ms"], cst["dequeue_p95_ms"],
               lat_noise, info=latency_info),
        # 口径不一致或任一侧无样本时，总量与占比同样是不可比量，必须标 info，
        # 不能把缺失侧按 0 判成“等待变短”。
        metric("dequeue_total_per_step_ms", "ms", bst["dequeue_total_per_step_ms"],
               cst["dequeue_total_per_step_ms"], noise, info=latency_info),
        metric("dequeue_queue_wait_share_pct", "pct",
               bst["dequeue_queue_wait_share_pct"], cst["dequeue_queue_wait_share_pct"],
               SHARE_NOISE_FLOOR_PCT, info=latency_info),
        metric("dequeue_pair_rate_pct", "pct", bst["pair_rate_pct"], cst["pair_rate_pct"],
               0.0, info=True),
    ]
    if no_kernels:
        metrics[3]["verdict"] = "info"
    summary = (f"每步排队等待 {bst['dequeue_total_per_step_ms']:.3f} → "
               f"{cst['dequeue_total_per_step_ms']:.3f}ms，等待 p50 "
               f"{bst['dequeue_p50_ms']:.3f} → {cst['dequeue_p50_ms']:.3f}ms，"
               f"配对率 {bst['pair_rate_pct']:.0f}% / {cst['pair_rate_pct']:.0f}%"
               f"（配对口径 base={b_mode} / cand={c_mode}）")
    common = [s for s in base["steps"] if s in cst["per_step"]]
    per_step = []
    for s in common:
        bs = cs = s
        per_step.append({"step": bs, "cand_step": cs,
                         "base": round4(bst["per_step"][bs]),
                         "cand": round4(cst["per_step"][cs]),
                         "delta": round4(bst["per_step"][bs] - cst["per_step"][cs])})
    step_note = None
    if len(base["steps"]) != len(cand["steps"]):
        step_note = (
            f"base/cand step 数不一致（{len(base['steps'])} vs {len(cand['steps'])}），"
            "per_step 按 step id 交集配对，仅两侧共同的 step 参与逐步比较"
        )
    return {"metrics": metrics, "per_step": per_step, "summary": summary,
            "base_stats": bst, "cand_stats": cst,
            "base_pairs": b_pairs, "cand_pairs": c_pairs,
            "base_mode": b_mode, "cand_mode": c_mode, "step_note": step_note}


def _mode_desc(mode):
    if mode == "exact":
        return "exact（dequeue.start − enqueue.end，correlation_id 配对）"
    return "heuristic（kernel.start − host.end，时间窗配对）"


def envelope(base, cand, result, notes, degradations):
    degr = [f"base: {x}" for x in degradations[0]] + [f"cand: {x}" for x in degradations[1]]
    b_mode = result.get("base_mode", "heuristic")
    c_mode = result.get("cand_mode", "heuristic")
    if b_mode != c_mode:
        degr.append(
            f"配对口径不一致（base={b_mode}, cand={c_mode}）："
            "exact=dequeue−enqueue、heuristic=kernel−host，等待指标不可直接比较"
        )
    conf = "low" if any("未找到 ProfilerStep" in x for x in degr) else (
        "medium" if degr else "high")
    if result["base_stats"]["pair_rate_pct"] == 0 or result["cand_stats"]["pair_rate_pct"] == 0:
        conf = "low"
    elif (result["base_stats"]["pair_rate_pct"] < 50.0
          or result["cand_stats"]["pair_rate_pct"] < 50.0
          or result["base_stats"].get("pair_count", 0) < 30
          or result["cand_stats"].get("pair_count", 0) < 30):
        # 配对率低或样本少时不能给 high；分位数结论需与样本量绑定。
        conf = "medium" if conf == "high" else conf
    if b_mode != c_mode:
        conf = "low"
    note_list = [x for pair in notes for x in pair]
    note_list.append(
        f"base 等待口径: {_mode_desc(b_mode)}；cand 等待口径: {_mode_desc(c_mode)}"
    )
    if result.get("step_note"):
        note_list.append(result["step_note"])
    note_list += [
        "不含 host 侧 API 自身耗时（见 -compare-enqueue）；配对率与配对口径写入指标/备注",
        "等待占比 = 总等待 / (总等待 + 设备忙碌)，用于判断排队是否是主要矛盾",
    ]
    return {
        "skill": "kptools-ascend-profiling-compare-dequeue",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": base["path"], "cand": cand["path"],
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
        f"<td>{m['delta']:+.3f}</td><td><span class='verdict {_cls(m['verdict'])}'>"
        f"{esc(m['verdict'])}</span></td><td>±{m['noise_band']}</td></tr>"
        for m in env["metrics"])
    table = ("<div class='sect'><h3>排队等待指标</h3><table><tr><th>指标</th><th>base</th>"
             "<th>cand</th><th>Δ</th><th>判定</th><th>噪声带</th></tr>" + rows + "</table></div>")
    slow = "".join(f"<tr><td>{esc(h['host']['name'])}</td><td>{h['latency_us']:.1f}</td></tr>"
                   for h in result["cand_stats"]["top_latency"])
    top = ("<div class='sect'><h3>候选侧等待最长的下发</h3><table><tr><th>API</th>"
           f"<th>等待(µs)</th></tr>{slow}</table></div>" if slow else "")
    deg = ("<div class='sect'><h3>降级与口径</h3><ul>"
           + "".join(f"<li>{esc(x)}</li>" for x in env["data_quality"]["degraded"] + env["notes"])
           + "</ul></div>")
    hero = ("<div class='hero'>"
            f"<div class='htitle'>Host Dequeue 对比 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='kv'>契约置信度: {esc(env['data_quality']['confidence'])}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>Host Dequeue 对比</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>Host Dequeue（排队等待）对比</h1>"
            f"{hero}{table}{top}{deg}</div></body></html>")


def render_error(msg, base, cand, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>Dequeue 对比失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>Dequeue 对比失败</h1>"
            f"<div class='hero'><div class='htitle'>无法完成对比</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base)}</div><div class='kv'>cand: {esc(cand)}</div>"
            "</div></body></html>")


def main(argv=None):
    ap = argparse.ArgumentParser(description="两份 Ascend profiling 的 host dequeue（排队等待）对比")
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="dequeue_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--pair-window-ms", type=float, default=5.0)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    args = ap.parse_args(argv)

    css_path = Path(__file__).resolve().parent.parent / "assets" / "report.css"
    nb, nc, db, dc = [], [], [], []
    try:
        base = load_capture(args.base, nb, db)
        cand = load_capture(args.cand, nc, dc)
        result = compare(base, cand, args.min_delta_ms, args.pair_window_ms)
    except Exception as exc:
        Path(args.out_html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_html).write_text(render_error(f"{type(exc).__name__}: {exc}",
                                                    args.base, args.cand, css_path), encoding="utf-8")
        print(f"对比失败: {exc}", file=sys.stderr)
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
