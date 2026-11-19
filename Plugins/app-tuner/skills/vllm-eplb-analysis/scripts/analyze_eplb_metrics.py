#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyse whether a vLLM MoE service is genuinely expert-load imbalanced.

Combines independent signals instead of trusting one number:

  I1 balancedness      EPLB log  avg_tokens/max_tokens  (vLLM eplb_state)
  I2 rank queue skew   vllm:num_requests_running + num_requests_waiting per engine
  I3 rank TTFT skew    vllm:time_to_first_token_seconds per engine (mean / p95)
  I4 prompt skew       prompt-token deltas per engine
  I5 persistence       how often balancedness stays below the "well balanced" line
  M0 memory headroom   KV-cache occupancy + preemptions (+ optional device free memory / bytes per expert),
                       used as a gate: imbalance is real but EPLB copies may not fit

Inputs (any mix):
  --evidence   normalised JSON (see references/eplb-metrics.md)
  --eplb-log   vLLM server log containing `EPLB step: ... balancedness=...` lines
  --prometheus `curl /metrics` text dump (per-engine labels are used)

Output: JSON verdict on stdout (and optionally --out file).

Verdicts:
  imbalanced    >=2 signals and the imbalance persists      -> hand off to optimization
  borderline    1 signal, or 2+ but not persistent          -> run the force-EPLB ceiling probe first
  balanced      0 signals                                   -> do NOT enable EPLB
  inconclusive  too little usable evidence                  -> collect more

The `force` probe is the cheap discriminator: EPLB only helps when imbalance is the
limiter, so if force-EPLB is no faster than `off`, expert placement is not your
bottleneck no matter what the balancedness number says.
"""

import argparse
import json
import re
import statistics
import sys

DEFAULTS = {
    "balancedness_imbalanced": 0.90,   # avg/max below this = real imbalance
    "balancedness_healthy": 0.95,      # avg/max at or above this = well balanced
    "queue_ratio": 1.50,               # max/median of in-flight requests
    "ttft_ratio": 1.30,                # max/median of per-engine mean TTFT
    "prompt_ratio": 1.20,              # max/median of per-engine prompt tokens
    "persistence": 0.60,               # share of windows that must be off-balance
    "min_windows": 3,                  # balancedness samples needed to judge
}

EPLB_LINE = re.compile(
    r"EPLB step:\s*(?P<step>\d+)\s+for model\s+(?P<model>\S+):\s*"
    r"avg_tokens=(?P<avg>[0-9.]+),\s*max_tokens=(?P<max>\d+),\s*"
    r"balancedness=(?P<bal>[0-9.]+)"
)

PROM_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[-+0-9.eE]+)\s*$"
)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- parsers
def parse_eplb_log(path):
    """Return {model: [balancedness...]} from vLLM EPLB balancedness log lines."""
    series = {}
    if not path:
        return series
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = EPLB_LINE.search(line)
            if not m:
                continue
            series.setdefault(m.group("model"), []).append(float(m.group("bal")))
    return series


def parse_prometheus(path):
    """Return {(metric, engine): value} and histogram pieces.

    Buckets are kept as {(metric, engine): {le: cumulative}}, plus _sum/_count.
    """
    if not path:
        return {}
    samples = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PROM_LINE.match(line)
            if not m:
                continue
            name = m.group("name")
            labels = m.group("labels") or ""
            engine = None
            me = re.search(r'engine="([^"]+)"', labels)
            if me:
                engine = me.group(1)
            le = re.search(r'le="([^"]+)"', labels)
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            if le:
                key = (name, engine, le.group(1))
            else:
                key = (name, engine)
            samples[key] = value
    return samples


def by_engine(samples, metric):
    out = {}
    for key, value in samples.items():
        if len(key) != 2:
            continue
        name, engine = key
        if name == metric and engine is not None:
            out[engine] = out.get(engine, 0.0) + value
    return out


def hist_by_engine(samples, metric):
    """Rebuild per-engine histogram: {engine: {"buckets": [(le, cum)], "sum": x, "count": n}}."""
    out = {}
    for key, value in samples.items():
        if len(key) != 3:
            continue
        name, engine, le = key
        if name != metric + "_bucket" or engine is None:
            continue
        d = out.setdefault(engine, {"buckets": [], "sum": 0.0, "count": 0.0})
        if le == "+Inf":
            d["buckets"].append((float("inf"), value))
        else:
            d["buckets"].append((float(le), value))
    for key, value in samples.items():
        if len(key) != 2:
            continue
        name, engine = key
        if engine is None:
            continue
        if name == metric + "_sum":
            out.setdefault(engine, {"buckets": [], "sum": 0.0, "count": 0.0})["sum"] = value
        elif name == metric + "_count":
            out.setdefault(engine, {"buckets": [], "sum": 0.0, "count": 0.0})["count"] = value
    for d in out.values():
        d["buckets"].sort(key=lambda b: b[0])
    return out


def hist_quantile(hist, q):
    buckets = hist["buckets"]
    total = hist["count"] or (buckets[-1][1] if buckets else 0)
    if not buckets or total <= 0:
        return None
    target = q * total
    prev_le, prev_cum = 0.0, 0.0
    for le, cum in buckets:
        if cum >= target:
            if le == float("inf"):
                return prev_le
            width = le - prev_le
            span = cum - prev_cum
            if span <= 0:
                return le
            return prev_le + width * (target - prev_cum) / span
        prev_le, prev_cum = le, cum
    return prev_le


# ---------------------------------------------------------------- indicators
def memory_block(evidence, prom):
    """Memory-headroom gate: can the service host redundant experts at all?"""
    kv = []
    if evidence.get("kv_cache_usage_pct") is not None:
        kv = [float(evidence["kv_cache_usage_pct"])]
    else:
        for v in by_engine(prom, "vllm:kv_cache_usage_perc").values():
            kv.append(v * 100.0 if v <= 1.0 else v)
    preempt = evidence.get("preemptions")
    if preempt is None:
        s = by_engine(prom, "vllm:num_preemptions")
        preempt = sum(s.values()) if s else None
    free_gib = evidence.get("free_mem_gib")
    per_expert = evidence.get("bytes_per_expert_gib")
    ep = evidence.get("ep_size")
    affordable = None
    if free_gib is not None and per_expert and per_expert > 0:
        per_rank = int(free_gib // per_expert)
        affordable = per_rank * int(ep) if ep else per_rank

    block = {
        "kv_cache_usage_pct": round(max(kv), 1) if kv else None,
        "preemptions": preempt,
        "free_mem_gib": free_gib,
        "bytes_per_expert_gib": per_expert,
        "affordable_redundant": affordable,
    }
    if not kv and preempt is None and free_gib is None:
        block["verdict"] = "unknown"
        return block
    if ((kv and max(kv) >= 95) or (preempt is not None and preempt > 0) or
            (free_gib is not None and free_gib <= 0.5) or
            (affordable is not None and affordable <= 0)):
        block["verdict"] = "no_headroom"
    elif ((kv and max(kv) >= 85) or (affordable is not None and affordable < 8)):
        block["verdict"] = "tight"
    else:
        block["verdict"] = "ample"
    return block


def ratio_stats(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    med = statistics.median(vals)
    if med <= 0:
        return None
    return max(vals) / med


def analyse(evidence, eplb_series, prom):
    params = dict(DEFAULTS)
    params.update(evidence.get("thresholds") or {})
    notes = []
    indicators = {}
    signals = []

    # I1 -- balancedness
    series = []
    if evidence.get("balancedness"):
        series.extend(float(x) for x in evidence["balancedness"])
    for values in eplb_series.values():
        series.extend(values)
    pers = None
    if series:
        med = statistics.median(series)
        pers = sum(1 for v in series if v < params["balancedness_healthy"]) / len(series)
        indicators["balancedness"] = {
            "windows": len(series),
            "median": round(med, 4),
            "min": round(min(series), 4),
            "persistence": round(pers, 3),
        }
        if med < params["balancedness_imbalanced"]:
            signals.append("I1_balancedness_low")
        elif med < params["balancedness_healthy"]:
            notes.append("balancedness 处于边缘区间（0.90~0.95），单看它不足以判定")
    else:
        indicators["balancedness"] = None
        notes.append("没有 balancedness 数据：请在 eplb_config 打开 log_balancedness")

    # I2 -- in-flight request skew
    waiting = dict(by_engine(prom, "vllm:num_requests_waiting"))
    running = dict(by_engine(prom, "vllm:num_requests_running"))
    if evidence.get("rank_waiting"):
        waiting = {str(i): float(v) for i, v in enumerate(evidence["rank_waiting"])}
    if evidence.get("rank_running"):
        running = {str(i): float(v) for i, v in enumerate(evidence["rank_running"])}
    inflight = {k: waiting.get(k, 0.0) + running.get(k, 0.0) for k in set(waiting) | set(running)}
    r = ratio_stats(inflight.values())
    if r is None:
        indicators["queue_skew"] = None
    else:
        indicators["queue_skew"] = {"ratio": round(r, 3), "max": max(inflight.values()),
                                    "median": statistics.median(inflight.values())}
        if r >= params["queue_ratio"]:
            signals.append("I2_queue_skew")

    # I3 -- per-engine TTFT skew
    ttft = {}
    if evidence.get("rank_ttft_ms"):
        ttft = {str(i): float(v) for i, v in enumerate(evidence["rank_ttft_ms"])}
    else:
        hist = hist_by_engine(prom, "vllm:time_to_first_token_seconds")
        for engine, h in hist.items():
            mean = (h["sum"] / h["count"]) if h["count"] else hist_quantile(h, 0.5)
            if mean is not None:
                ttft[engine] = mean * 1000.0
    r = ratio_stats(ttft.values())
    if r is None:
        indicators["ttft_skew"] = None
    else:
        indicators["ttft_skew"] = {"ratio": round(r, 3), "max_ms": round(max(ttft.values()), 1),
                                   "median_ms": round(statistics.median(ttft.values()), 1)}
        if r >= params["ttft_ratio"]:
            signals.append("I3_ttft_skew")

    # I4 -- prompt token skew
    prompt = {}
    if evidence.get("rank_prompt_tokens"):
        prompt = {str(i): float(v) for i, v in enumerate(evidence["rank_prompt_tokens"])}
    r = ratio_stats(prompt.values())
    if r is None:
        indicators["prompt_skew"] = None
    else:
        indicators["prompt_skew"] = {"ratio": round(r, 3)}
        if r >= params["prompt_ratio"]:
            signals.append("I4_prompt_skew")

    # confounder: uniform device utilisation means placement is not the limiter
    util = evidence.get("rank_util_pct")
    if util and len(util) >= 2:
        spread = max(util) - min(util)
        indicators["util_spread_pct"] = round(spread, 1)
        if spread <= 5 and min(util) >= 80:
            notes.append("各 rank 利用率都接近打满且差异 <=5%：瓶颈更像整体算力/带宽，不是专家摆放")

    memory = memory_block(evidence, prom)
    indicators["memory"] = memory

    usable = sum(1 for k in ("balancedness", "queue_skew", "ttft_skew", "prompt_skew")
                 if indicators.get(k))
    windows = indicators["balancedness"]["windows"] if indicators.get("balancedness") else 0
    persistent = pers is None or pers >= params["persistence"]

    if usable < 2 or (indicators.get("balancedness") and windows < params["min_windows"]):
        verdict = "inconclusive"
        action = "collect_more"
    elif len(signals) >= 2 and persistent:
        verdict = "imbalanced"
        action = "optimize_dynamic"
        if memory["verdict"] == "no_headroom":
            action = "free_memory_first"
        elif memory["verdict"] == "tight" and memory.get("affordable_redundant") is not None:
            action = "optimize_with_cap"
    elif signals:
        verdict = "borderline"
        action = "probe_ceiling_first"
    else:
        verdict = "balanced"
        action = "do_not_enable"

    return {
        "verdict": verdict,
        "signals": signals,
        "indicators": indicators,
        "notes": notes,
        "recommendation": recommend(action, indicators, evidence),
    }


def recommend(action, indicators, evidence):
    mem = indicators.get("memory") or {}
    affordable = mem.get("affordable_redundant")

    if action == "do_not_enable":
        return {
            "action": action,
            "reason": "没有可判定的失衡信号；此时开 EPLB 只会增加副本显存与重排抖动。",
            "next": "保持 off；若怀疑长尾，先查 TTFT 直方图与队列，而不是加专家副本。",
        }
    if action == "collect_more":
        return {
            "action": action,
            "reason": "可用指标不足或窗口太短，无法区分「真失衡」与「采样噪声」。",
            "next": "打开 eplb_config.log_balancedness（interval 1）跑 >=3 个窗口；同时导出一份 /metrics 文本。",
        }
    if action == "probe_ceiling_first":
        return {
            "action": action,
            "reason": "只有单一/不稳定信号。用 force EPLB 做上限探针，比直接上线 dynamic 更安全："
                      "force 与 off 差距大才说明失衡确实是瓶颈。",
            "probe": {"skill": "vllm-eplb-optimization", "mode": "force",
                      "note": "只作测量，不作上线配置；测完还原。"},
            "next": "probe 显示 force 明显快于 off 后，再按 optimize_dynamic 执行。",
        }
    if action == "free_memory_first":
        return {
            "action": action,
            "reason": "失衡是真的，但显存已没有余量（KV 占用/换出/剩余显存 触顶）。冗余专家会挤占 KV cache，"
                      "此时直接加副本大概率把长尾换成 OOM 或换出。",
            "memory": mem,
            "next": "先降 KV 压力（减小 max-model-len、降并发或缩短上下文），或把冗余专家数降到显存能容纳的量；"
                    "再复跑本分析确认仍有失衡，才进入 dynamic。",
        }

    bal = (indicators.get("balancedness") or {}).get("median")
    suggested = 32 if (bal is not None and bal < 0.80) else 16
    extra = suggested
    capped = False
    if affordable is not None and affordable < extra:
        extra = max(0, int(affordable))
        capped = True
    handoff = {
        "skill": "vllm-eplb-optimization",
        "mode": "dynamic",
        "redundant_experts": extra,
        "window_size": 1000,
        "step_interval": 1000,
    }
    notes = ["按默认 1000 步窗口起步；若压测计划短于该周期，把 window_size/step_interval 调小到压测步数以内，"
             "否则不会触发重排。"]
    if capped:
        notes.append("按显存余量把冗余专家从 %d 压到 %d；每减少一个副本都会削弱削峰能力，"
                     "若收益不足，优先释放显存而不是继续加副本。" % (suggested, extra))
    if action == "optimize_with_cap":
        return {
            "action": action,
            "reason": "失衡成立，但显存余量偏紧；可以开 dynamic，冗余专家需要按余量收敛。",
            "memory": mem,
            "handoff": handoff,
            "next": " ".join(notes),
        }
    return {
        "action": "optimize_dynamic",
        "reason": "多个独立信号一致且失衡持续，专家摆放是可控的瓶颈之一。",
        "memory": mem,
        "handoff": handoff,
        "next": " ".join(notes),
    }


def load_evidence(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        die("ERROR: cannot read evidence JSON %s: %s" % (path, e))


def main():
    ap = argparse.ArgumentParser(description="Judge MoE expert-load imbalance from multiple signals")
    ap.add_argument("--evidence", help="normalised evidence JSON")
    ap.add_argument("--eplb-log", help="vLLM log with `EPLB step: ... balancedness=` lines")
    ap.add_argument("--prometheus", help="text dump of vLLM /metrics")
    ap.add_argument("--out", help="write JSON verdict here")
    ap.add_argument("--expect", choices=["imbalanced", "borderline", "balanced", "inconclusive"],
                    help="exit 0 only if verdict matches (for CI)")
    args = ap.parse_args()

    if not any((args.evidence, args.eplb_log, args.prometheus)):
        die("ERROR: give at least one of --evidence / --eplb-log / --prometheus")

    evidence = load_evidence(args.evidence)
    result = analyse(evidence, parse_eplb_log(args.eplb_log), parse_prometheus(args.prometheus))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.expect:
        return 0 if result["verdict"] == args.expect else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
