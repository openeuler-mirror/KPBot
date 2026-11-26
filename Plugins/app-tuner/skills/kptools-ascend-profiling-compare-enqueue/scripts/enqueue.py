#!/usr/bin/env python3
"""Host 下发（enqueue）耗时统计：per-op 下发耗时、下发→设备启动延迟、每步下发总量、重叠比。

输出 A.1 信封 + HTML。数据源为 trace_view.json（host API 事件 + 设备 kernel 事件），
按「host 下发完成 → 同窗口内首个设备 kernel 启动」配对，配对率写入报告。

用法:
  enqueue.py <base> <cand> [--out-html r.html] [--json r.json]
             [--pair-window-ms 5.0] [--min-delta-ms 0.05]
"""

import argparse
import bisect
import json
import re
import sys
from pathlib import Path

AI_TASK_TYPES = {"AI_CORE", "AI_VECTOR_CORE", "MIX_AIC", "MIX_AIV", "AI_CPU", "DSA_SQE"}
HOST_CATS = {"pytorch_api", "cann_api", "cann", "operator", "enqueue"}
# 回退路径（无 cat=enqueue）里这些 API 是阻塞/查询/资源管理，不是下发；
# 计入会把同步等待当成 enqueue（实测一条 30ms sync 可把每步下发放大 300 倍）。
NON_ENQUEUE_HINTS = (
    "synchronize", "query", "getdevice", "setdevice", "malloc", "free",
    "eventrecord", "eventwait", "notifyrecord", "notifywait", "streamwait",
)
SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 512 * 1024 * 1024
TRACE_TRUNCATE_BYTES = 256 * 1024 * 1024


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


def _is_host_api(name, cat):
    low = str(name).lower()
    if str(cat).lower() == "enqueue" or low.startswith("enqueue@"):
        return True
    if any(h in low for h in NON_ENQUEUE_HINTS):
        return False
    return (str(cat).lower() in HOST_CATS or low.startswith("ascendcl@")
            or "aclnn" in low)


def _task_type(e):
    a = e.get("args")
    if isinstance(a, dict):
        for k, v in a.items():
            if re.sub(r"[^a-z]", "", str(k).lower()) == "tasktype":
                return str(v)
    return ""


def _device_task(e, name, cat):
    tt = _task_type(e)
    if tt:
        return tt in AI_TASK_TYPES or tt in ("SDMA_SQE", "PCIE_DMA_SQE") or name.startswith("hcom")
    return cat in AI_TASK_TYPES or name.startswith("hcom")


MAX_TRACE_BYTES = 512 * 1024 * 1024


def _extract_complete_events(text):
    """从超大 trace 文本前缀中提取完整的顶层事件对象，避免整文件 json.loads OOM。"""
    start = text.find("[")
    if start < 0:
        return []
    depth, in_str, esc, obj_start = 0, False, False, None
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


def load_capture(path, notes, degradations):
    output_dir, trace_path = locate(path)
    if not trace_path.exists():
        raise FileNotFoundError(f"未找到 trace_view.json: {trace_path}")
    size = trace_path.stat().st_size
    if size > MAX_TRACE_BYTES:
        raise FileNotFoundError(
            f"trace_view.json 过大（{size / 1e6:.0f}MB > {MAX_TRACE_BYTES / 1e6:.0f}MB），"
            "为避免 OOM 拒绝整文件加载；请先切分 trace 或改用 kernel_details.csv/db 路径"
        )
    size = trace_path.stat().st_size
    if size > MAX_TRACE_BYTES:
        degradations.append(
            f"trace_view.json 过大（{size / 1e6:.0f}MB > {MAX_TRACE_BYTES / 1e6:.0f}MB），"
            f"仅解析前 {TRACE_TRUNCATE_BYTES / 1e6:.0f}MB 的完整事件，指标可能偏小"
        )
        try:
            with open(trace_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(TRACE_TRUNCATE_BYTES)
            events = _extract_complete_events(text)
        except OSError as exc:
            raise FileNotFoundError(f"trace_view.json 读取失败: {exc}")
    else:
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

    hosts, kernels = [], []
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
        step = None
        for sid, a, b in spans:
            if a <= ts < b:
                step = sid
                break
        if step is None and not spans:
            step = 0
        if _device_task(e, name, cat):
            kernels.append({"start": ts, "end": ts + dur, "name": name[:120], "step": step})
        elif _is_host_api(name, cat):
            # 名字带 Enqueue@ 也视为精确 enqueue，避免仅凭 cat 判定来源。
            precise = str(cat).lower() == "enqueue" or name.lower().startswith("enqueue@")
            hosts.append({"start": ts, "end": ts + dur, "dur": dur,
                          "name": name.split("@", 1)[-1][:120],
                          "cat": "enqueue" if precise else cat, "step": step})
    enq_only = [h for h in hosts if h.get("cat") == "enqueue"]
    if enq_only:
        hosts = enq_only
    if not hosts and not kernels:
        raise FileNotFoundError("trace 内既无 host API 事件也无设备 kernel")
    if not hosts:
        degradations.append("未识别到 host API 事件，无法统计下发耗时")
    if not kernels:
        degradations.append("未识别到设备 kernel，无法配对下发→启动延迟")
    if any(h["cat"] == "enqueue" for h in hosts):
        notes.append("host 下发来自 trace 的 cat=enqueue 事件（精确）")
    elif hosts:
        degradations.append(
            "未发现 cat=enqueue，host 下发来自 API 名/类别启发式（可能含非 enqueue 调用）"
        )
    outside = sum(1 for h in hosts if h["step"] is None)
    if outside:
        notes.append(f"{outside} 个 host 事件不在 ProfilerStep 窗口内，未计入 per-step")
    # step 全集使用 ProfilerStep#N 的真实编号（不是位置序号），避免 1 起始 trace 整体错位。
    steps = sorted({sid for sid, _a, _b in spans}) if spans else [0]
    notes.append(f"host API {len(hosts)} 条、设备 kernel {len(kernels)} 条")
    return {"path": str(trace_path), "source": "trace_view.json", "hosts": hosts,
            "kernels": kernels, "steps": steps,
            "window": (min((h["start"] for h in hosts), default=0.0),
                       max((h["end"] for h in hosts), default=0.0))}


def pair_enqueues(cap, window_ms):
    """host 下发完成 → 窗口内首个未匹配设备 kernel（µs）。

    对每个 host 用 bisect 在已排序 kernel 起点上定位 >= host.end 的位置，
    再在其后 window 内找第一个未消费 kernel；避免长下发把后续短下发要用的
    kernel 越过（单调指针在 host.end 不单调时会漏配）。
    """
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
    return pairs, len(used)


def stats(cap, pairs, paired):
    hosts = cap["hosts"]
    host_busy = cover_all([(h["start"], h["end"]) for h in hosts])
    kernel_busy = cover_all([(k["start"], k["end"]) for k in cap["kernels"]])
    overlap = intersect_len([(h["start"], h["end"]) for h in hosts],
                            [(k["start"], k["end"]) for k in cap["kernels"]])
    steps = max(1, len(cap["steps"]))
    durs = [h["dur"] / 1000.0 for h in hosts]
    lats = [p["latency_us"] / 1000.0 for p in pairs]
    per_step = {}
    for s in cap["steps"]:
        rows = [h for h in hosts if h["step"] == s]
        per_step[s] = sum(h["dur"] for h in rows) / 1000.0
    return {
        "enqueue_p50_ms": pctl(durs, 50), "enqueue_p95_ms": pctl(durs, 95),
        # 分母是 ProfilerStep 步数；无下发的 step 以 0 计入，窗口外 host 不计入。
        "enqueue_total_per_step_ms": sum(per_step.values()) / steps,
        "to_start_p50_ms": pctl(lats, 50), "to_start_p95_ms": pctl(lats, 95),
        "pair_rate_pct": (paired / len(hosts) * 100.0) if hosts else 0.0,
        "overlap_pct": (overlap / host_busy * 100.0) if host_busy > 0 else 0.0,
        "host_busy_us": host_busy, "kernel_busy_us": kernel_busy,
        "per_step": per_step, "host_count": len(hosts), "durs_ms": durs, "lats_ms": lats,
        "top_slow": sorted(hosts, key=lambda h: -h["dur"])[:10],
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


def compare(base, cand, min_delta_ms, window_ms=5.0, min_delta_pct=1.0):
    b_pairs, b_paired = pair_enqueues(base, window_ms)
    c_pairs, c_paired = pair_enqueues(cand, window_ms)
    bst = stats(base, b_pairs, b_paired)
    cst = stats(cand, c_pairs, c_paired)
    common = [s for s in base["steps"] if s in cst["per_step"]]
    b_series = [bst["per_step"][s] for s in common]
    c_series = [cst["per_step"][s] for s in common]
    # 指标与噪声带都使用共同 step 的同一组样本，避免判定被多出的一步驱动。
    if common:
        b_total = sum(b_series) / len(common)
        c_total = sum(c_series) / len(common)
    else:
        b_total = bst["enqueue_total_per_step_ms"]
        c_total = cst["enqueue_total_per_step_ms"]
    noise = max(min_delta_ms, mad(b_series) + mad(c_series))
    noise_dur = max(min_delta_ms, mad(bst["durs_ms"]) + mad(cst["durs_ms"]))
    noise_lat = max(min_delta_ms, mad(bst["lats_ms"]) + mad(cst["lats_ms"]))
    latency_ok = bool(bst["lats_ms"]) and bool(cst["lats_ms"])
    latency_verdict_info = not latency_ok

    metrics = [
        metric("enqueue_p50_ms", "ms", bst["enqueue_p50_ms"], cst["enqueue_p50_ms"], noise_dur),
        metric("enqueue_p95_ms", "ms", bst["enqueue_p95_ms"], cst["enqueue_p95_ms"], noise_dur),
        metric("enqueue_total_per_step_ms", "ms", b_total, c_total, noise),
        metric("enqueue_to_start_p50_ms", "ms", bst["to_start_p50_ms"], cst["to_start_p50_ms"],
               noise_lat, info=latency_verdict_info),
        metric("enqueue_to_start_p95_ms", "ms", bst["to_start_p95_ms"], cst["to_start_p95_ms"],
               noise_lat, info=latency_verdict_info),
        metric("enqueue_pair_rate_pct", "pct", bst["pair_rate_pct"], cst["pair_rate_pct"],
               0.0, info=True),
        # 重叠率是百分比，用最小百分点阈值避免 0.01% 抖动被判定。
        metric("dispatch_overlap_pct", "pct", bst["overlap_pct"], cst["overlap_pct"],
               max(0.0, min_delta_pct), invert=True),
    ]
    summary = (f"每步下发 {b_total:.3f} → "
               f"{c_total:.3f}ms，下发→启动 p50 "
               f"{bst['to_start_p50_ms']:.3f} → {cst['to_start_p50_ms']:.3f}ms，"
               f"配对率 {bst['pair_rate_pct']:.0f}% / {cst['pair_rate_pct']:.0f}%")
    per_step = []
    for s in common:
        per_step.append({"step": s, "cand_step": s,
                         "base": round4(bst["per_step"][s]),
                         "cand": round4(cst["per_step"][s]),
                         "delta": round4(bst["per_step"][s] - cst["per_step"][s])})
    notes = []
    if not latency_ok:
        notes.append("一侧没有有效配对，下发→启动延迟分位数标为不可比较")
    if base["steps"] and cand["steps"] and not common:
        notes.append("两侧 step id 无交集，per_step 为空；请确认两份采集的 step 语义一致")
    if len(base["steps"]) != len(cand["steps"]):
        notes.append(
            f"步数不一致（base {len(base['steps'])} / cand {len(cand['steps'])}），只比较交集"
        )
    for tag, st in (("base", bst), ("cand", cst)):
        if st["host_count"] and st["pair_rate_pct"] < 50.0:
            notes.append(
                f"{tag} 配对率 {st['pair_rate_pct']:.0f}% < 50%，下发→启动延迟结论需打折"
            )
    return {"metrics": metrics, "per_step": per_step, "summary": summary, "notes": notes,
            "base_stats": bst, "cand_stats": cst,
            "base_pairs": b_pairs, "cand_pairs": c_pairs}


def envelope(base, cand, result, notes, degradations):
    degr = [f"base: {x}" for x in degradations[0]] + [f"cand: {x}" for x in degradations[1]]
    conf = "low" if any("未找到 ProfilerStep" in x for x in degr) else (
        "medium" if degr else "high")
    if result["base_stats"]["pair_rate_pct"] == 0 or result["cand_stats"]["pair_rate_pct"] == 0:
        conf = "low"
    return {
        "skill": "kptools-ascend-profiling-compare-enqueue",
        "schema_version": SCHEMA_VERSION,
        "mode": "pair",
        "inputs": {"base": base["path"], "cand": cand["path"],
                   "steps": f"per_step={len(result['per_step'])}"},
        "data_quality": {"sidecars": ["trace_view.json"], "degraded": degr, "confidence": conf},
        "metrics": result["metrics"],
        "per_step": result["per_step"],
        "summary": result["summary"],
        "notes": [f"base: {x}" for x in notes[0]] + [f"cand: {x}" for x in notes[1]]
        + list(result.get("notes") or []) + [
            "下发耗时 = host API 事件时长；下发→启动 = 设备 kernel 起点 − host API 终点",
            "配对为启发式（同窗口内首个未匹配 kernel），配对率写进指标",
            "每步下发以 ProfilerStep 步数为分母，无下发的 step 计 0",
        ],
    }


def validate(env):
    errs = []
    for k in ("skill", "schema_version", "mode", "inputs", "data_quality",
              "metrics", "per_step", "summary", "notes"):
        if k not in env:
            errs.append(f"缺 {k}")
    for i, m in enumerate(env.get("metrics") or []):
        if m.get("verdict") not in ("improved", "regressed", "noise", "info"):
            errs.append(f"metrics[{i}].verdict 非法")
    if not env.get("metrics"):
        errs.append("metrics 为空")
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
    table = ("<div class='sect'><h3>下发指标</h3><table><tr><th>指标</th><th>base</th>"
             "<th>cand</th><th>Δ</th><th>判定</th><th>噪声带</th></tr>" + rows + "</table></div>")
    slow = "".join(f"<tr><td>{esc(h['name'])}</td><td>{h['dur']:.1f}</td></tr>"
                   for h in result["cand_stats"]["top_slow"])
    top = ("<div class='sect'><h3>候选侧下发最慢的 host API</h3><table><tr><th>API</th>"
           f"<th>耗时(µs)</th></tr>{slow}</table></div>" if slow else "")
    deg = ("<div class='sect'><h3>降级与口径</h3><ul>"
           + "".join(f"<li>{esc(x)}</li>" for x in env["data_quality"]["degraded"] + env["notes"])
           + "</ul></div>")
    hero = ("<div class='hero'>"
            f"<div class='htitle'>Host 下发对比 · <span class='judge'>{esc(env['summary'])}</span></div>"
            f"<div class='kv'>契约置信度: {esc(env['data_quality']['confidence'])}</div></div>")
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>Host 下发对比</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>Host 下发（enqueue）对比</h1>"
            f"{hero}{table}{top}{deg}</div></body></html>")


def render_error(msg, base, cand, css_path):
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>下发对比失败</title>"
            f"<style>{css}</style></head><body><div class='page on'><h1>下发对比失败</h1>"
            f"<div class='hero'><div class='htitle'>无法完成对比</div>"
            f"<div class='hnums'>{esc(msg)}</div></div>"
            f"<div class='kv'>base: {esc(base)}</div><div class='kv'>cand: {esc(cand)}</div>"
            "</div></body></html>")


def _input_files(*roots):
    names = ("trace_view.json", "analysis.db", "kernel_details.csv",
             "step_trace_time.csv", "communication.json", "api_statistic.csv",
             "api_detail.csv")
    out = set()
    for root in roots:
        p = Path(root).expanduser()
        if p.is_file():
            out.add(p.resolve())
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and (f.name in names or f.name.startswith(
                        ("ascend_pytorch_profiler_", "profiler_info_"))):
                    out.add(f.resolve())
    return out


def _guard_outputs(base, cand, out_html, out_json, overwrite):
    """拒绝符号链接、越界路径、覆盖输入侧车，以及未授权的既有文件覆盖。"""
    inputs = _input_files(base, cand)
    outs = [("--out-html", Path(out_html).expanduser())]
    if out_json:
        outs.append(("--json", Path(out_json).expanduser()))
    if out_json and Path(out_html).expanduser().resolve() == Path(out_json).expanduser().resolve():
        raise ValueError("--out-html 与 --json 不能指向同一文件")
    resolved = []
    for flag, p in outs:
        if p.is_symlink():
            raise ValueError(f"{flag} {p} 是符号链接，拒绝写入")
        rp = p.resolve()
        if rp in inputs:
            raise ValueError(f"{flag} {rp} 指向输入采集文件，拒绝覆盖")
        if rp.exists() and not overwrite:
            raise ValueError(f"{flag} {rp} 已存在，确认覆盖请加 --overwrite")
        resolved.append(rp)
    return resolved


def main(argv=None):
    ap = argparse.ArgumentParser(description="两份 Ascend profiling 的 host 下发（enqueue）对比")
    ap.add_argument("base")
    ap.add_argument("cand")
    ap.add_argument("--out-html", default="enqueue_report.html")
    ap.add_argument("--json", dest="out_json", default=None)
    ap.add_argument("--pair-window-ms", type=float, default=5.0)
    ap.add_argument("--min-delta-ms", type=float, default=0.05)
    ap.add_argument("--min-delta-pct", type=float, default=1.0,
                    help="重叠率的最小判定阈值（百分点）")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖已存在的输出文件（默认拒绝）")
    args = ap.parse_args(argv)

    try:
        out_html_path, *rest = _guard_outputs(
            args.base, args.cand, args.out_html, args.out_json, args.overwrite)
        out_json_path = rest[0] if rest else None
    except ValueError as exc:
        print(f"输出路径不安全，拒绝写入：{exc}", file=sys.stderr)
        return 3

    css_path = Path(__file__).resolve().parent.parent / "assets" / "report.css"
    nb, nc, db, dc = [], [], [], []
    try:
        base = load_capture(args.base, nb, db)
        cand = load_capture(args.cand, nc, dc)
        result = compare(base, cand, args.min_delta_ms, args.pair_window_ms,
                         args.min_delta_pct)
    except Exception as exc:
        Path(out_html_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_html_path).write_text(render_error(f"{type(exc).__name__}: {exc}",
                                                    args.base, args.cand, css_path), encoding="utf-8")
        print(f"对比失败: {exc}", file=sys.stderr)
        return 3

    env = envelope(base, cand, result, [nb, nc], [db, dc])
    errs = validate(env)
    if errs:
        print("警告：JSON 不符合 A.1: " + "; ".join(errs), file=sys.stderr)
    if out_json_path:
        Path(out_json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json_path).write_text(json.dumps(env, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"JSON → {out_json_path}")
    Path(out_html_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_html_path).write_text(render(env, result, css_path), encoding="utf-8")
    print(f"HTML → {out_html_path}")
    print(f"结论: {env['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
