#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recommend_redundancy.py -- sanity-check the redundant-expert ratio for EPLB.

EPLB's benefit comes from redundant expert copies; the cost is expert-weight
memory taken away from KV cache. This script turns a candidate number into the
numbers you actually need to decide:

  * redundant ratio (redundant / logical) and per-rank extra slots
  * the vLLM divisibility rule (redundant must be a multiple of ep_size)
  * incremental memory per rank / for the whole cluster, and whether it fits
  * a suggested band and the largest affordable value when it does not fit

Usage:
    python3 recommend_redundancy.py --logical-experts 384 --ep-size 16 \
        --redundant-experts 16
    python3 recommend_redundancy.py --logical-experts 384 --ep-size 16 \
        --ratio 0.04 --bytes-per-expert-gib 2.4 --free-gib 6
    python3 recommend_redundancy.py --logical-experts 384 --ep-size 16 \
        --redundant-experts 64 --free-gib 1

Notes:
  --bytes-per-expert-gib is the incremental cost of ONE redundant expert on
  ONE EP rank (DeepSeekV3 reference: ~2.4 GiB). For a per-layer figure use
  --bytes-per-expert-per-layer-gib together with --num-moe-layers; the script
  then applies  extra_per_rank = (R / E) * layers * bytes_per_layer.
"""

import argparse
import json
import sys


def round_up(value, multiple):
    if multiple <= 0:
        return value
    return ((value + multiple - 1) // multiple) * multiple


def analyse(args):
    n = args.logical_experts
    e = args.ep_size
    warnings = []

    if args.redundant_experts is not None:
        r = args.redundant_experts
    elif args.ratio is not None:
        r = int(round(n * args.ratio))
    else:
        r = 0

    raw = r
    r = round_up(r, e)
    if r != raw:
        warnings.append("num_redundant_experts 必须是 ep_size 的整数倍：%d 已向上取整为 %d" % (raw, r))

    ratio = (r / n) if n else 0.0
    per_rank_extra = r // e if e else 0
    physical_per_rank = (n + r) // e if e else n

    if ratio < 0.01:
        band = "low"
        warnings.append("冗余比例 < 1%：几乎没有多余副本可削峰，EPLB 只剩重排价值")
    elif ratio <= 0.12:
        band = "recommended"
    elif ratio <= 0.15:
        band = "high"
        warnings.append("冗余比例 > 12%：副本显存与重排搬运量偏高，确认收益后再加")
    else:
        band = "very_high"
        warnings.append("冗余比例 > 15%：通常先用上限探针验证收益，再决定是否长期保留")

    # --- memory ---
    bytes_per_rank = None
    if args.bytes_per_expert_per_layer_gib is not None:
        if args.num_moe_layers is None:
            warnings.append("给了每层单专家成本但没有 --num-moe-layers，无法换算显存")
        else:
            bytes_per_rank = args.bytes_per_expert_per_layer_gib * args.num_moe_layers
    elif args.bytes_per_expert_gib is not None:
        bytes_per_rank = args.bytes_per_expert_gib

    memory = None
    if bytes_per_rank is not None:
        extra_per_rank = per_rank_extra * bytes_per_rank
        extra_total = r * bytes_per_rank
        memory = {
            "bytes_per_extra_expert_per_rank_gib": round(bytes_per_rank, 3),
            "extra_per_rank_gib": round(extra_per_rank, 3),
            "extra_total_gib": round(extra_total, 3),
        }
        if args.free_gib is not None:
            fits = extra_per_rank <= args.free_gib
            memory["free_gib"] = args.free_gib
            memory["fits"] = fits
            if not fits:
                max_per_rank = int(args.free_gib // bytes_per_rank)
                memory["max_affordable_redundant_experts"] = max_per_rank * e
                warnings.append(
                    "显存不足：本配置每 rank 需要 %.2f GiB，可用只有 %.2f GiB；"
                    "本机至多放 %d 个冗余专家（%d/rank）"
                    % (extra_per_rank, args.free_gib, max_per_rank * e, max_per_rank))

    return {
        "logical_experts": n,
        "ep_size": e,
        "redundant_experts": r,
        "redundant_ratio_pct": round(ratio * 100, 2),
        "per_rank_extra_experts": per_rank_extra,
        "physical_experts_per_rank": physical_per_rank,
        "weight_overhead_pct": round(ratio * 100, 2),
        "band": band,
        "memory": memory,
        "warnings": warnings,
    }


def main():
    ap = argparse.ArgumentParser(description="Sanity-check the EPLB redundant-expert ratio")
    ap.add_argument("--logical-experts", type=int, required=True,
                    help="number of logical (routed) experts, e.g. 384")
    ap.add_argument("--ep-size", type=int, required=True, help="expert-parallel size")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--redundant-experts", type=int, help="candidate num_redundant_experts")
    g.add_argument("--ratio", type=float, help="candidate ratio, e.g. 0.04")
    ap.add_argument("--bytes-per-expert-gib", type=float,
                    help="incremental GiB of one redundant expert on one EP rank")
    ap.add_argument("--bytes-per-expert-per-layer-gib", type=float,
                    help="same, per MoE layer (needs --num-moe-layers)")
    ap.add_argument("--num-moe-layers", type=int)
    ap.add_argument("--free-gib", type=float, help="free per-device memory available for copies")
    ap.add_argument("--out", help="write JSON here")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the candidate does not fit or the band is not recommended")
    args = ap.parse_args()

    if args.logical_experts <= 0 or args.ep_size <= 0:
        print("ERROR: --logical-experts and --ep-size must be positive", file=sys.stderr)
        return 1
    if args.redundant_experts is not None and args.redundant_experts < 0:
        print("ERROR: --redundant-experts must be >= 0", file=sys.stderr)
        return 1
    if args.ratio is not None and args.ratio < 0:
        print("ERROR: --ratio must be >= 0", file=sys.stderr)
        return 1

    result = analyse(args)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    if args.strict:
        fits = (result["memory"] or {}).get("fits", True)
        if not fits or result["band"] in ("low", "very_high"):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
