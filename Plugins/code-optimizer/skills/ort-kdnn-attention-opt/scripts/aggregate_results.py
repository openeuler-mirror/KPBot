#!/usr/bin/env python3
"""Aggregate A/B benchmark logs into a stats table with CV gating.

Parses per-run logs produced by run_ab_benchmark.sh (filenames
<label>_<cfg>_r<round>.log). Handles BOTH harness output formats:
  - "Measured N iterations: total=.. s, average_latency=X ms, throughput=.."
  - "Measured N iterations: total=.. s, mean=X ms, median=Y ms, ..."
Per-config: round values -> median (of round means), CV% across rounds.
CV > 5% marks the config INVALID for conclusions.

Usage: python3 aggregate_results.py <logdir> [--label ab] [--cv-threshold 5]
"""
import argparse
import glob
import math
import os
import re
import statistics

MEAN_PATTERNS = [
    re.compile(r"average_latency=([0-9.]+)\s*ms"),
    re.compile(r"\bmean=([0-9.]+)\s*ms"),
]


def parse_latency(path):
    for line in open(path, errors="replace"):
        if "Measured" not in line:
            continue
        for pat in MEAN_PATTERNS:
            m = pat.search(line)
            if m:
                return float(m.group(1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logdir")
    ap.add_argument("--label", default="ab")
    ap.add_argument("--cv-threshold", type=float, default=5.0)
    args = ap.parse_args()

    runs = {}
    for path in sorted(glob.glob(os.path.join(args.logdir, f"{args.label}_*_r*.log"))):
        base = os.path.basename(path)
        m = re.match(rf"{re.escape(args.label)}_(\w+)_r(\d+)\.log", base)
        if not m:
            continue
        cfg, rnd = m.group(1), int(m.group(2))
        lat = parse_latency(path)
        if lat is not None:
            runs.setdefault(cfg, {})[rnd] = (lat, path)

    if not runs:
        print("no parseable logs found")
        return 1

    print(f"| config | rounds | mean_ms | median_ms | min_ms | max_ms | CV% | verdict |")
    print(f"|---|---|---|---|---|---|---|---|")
    results = {}
    for cfg in sorted(runs):
        vals = [v[0] for v in sorted(runs[cfg].values())]
        mean = statistics.mean(vals)
        med = statistics.median(vals)
        cv = (statistics.stdev(vals) / mean * 100) if len(vals) > 1 else 0.0
        ok = cv <= args.cv_threshold
        results[cfg] = (med, cv, ok)
        print(f"| {cfg} | {len(vals)} | {mean:.3f} | {med:.3f} | {min(vals):.3f} | "
              f"{max(vals):.3f} | {cv:.2f} | {'OK' if ok else 'CV>5% INVALID'} |")

    if len(results) == 2:
        (ca, (ma, cva, oka)), (cb, (mb, cvb, okb)) = list(results.items())[:2]
        if oka and okb and ma > 0:
            speedup = (ma - mb) / ma * 100
            print(f"\n{ca} -> {cb}: {(ma - mb):.3f} ms, {speedup:+.2f}% "
                  f"({'B 更快' if speedup > 0 else 'A 更快/无差异'})")
        else:
            print("\n(有配置 CV 超标,不给出结论 — 重跑)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
