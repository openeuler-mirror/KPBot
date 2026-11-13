#!/usr/bin/env python3
"""Value / outcome distribution analyser for hot-path specialisation.

Reads observed samples (one per line, CSV, or JSON) and reports:

  * the histogram, ordered by frequency,
  * the share of the most frequent value (dominance),
  * the smallest k such that the top-k values cover a target share,
  * a recommendation that maps directly onto the recipes of
    hot-path-overhead-elimination.

The point of the script is to keep "specialise this hot value" honest: a
rewrite is only worth generating when the observed distribution concentrates.
Guessing the distribution from source code is exactly what the skill forbids.

Supported inputs
----------------
text      one sample per line (numbers or labels), blank lines and # comments
          skipped.  Default when the suffix is not .json/.csv.
csv       comma separated; --column selects the field (default last).
json      either a list of samples, an object {"samples": [...]},
          or an object mapping value -> share/count.  When every value is a
          number <= 1 and the sum is close to 1 the mapping is treated as
          shares, otherwise as counts.

Usage
-----
  python3 value_distribution.py --input samples.txt --threshold 0.9
  python3 value_distribution.py --input outs.json --json
  python3 value_distribution.py --input run.csv --column 2 --max-k 4
"""

import argparse
import csv
import json
import sys
from collections import Counter

SHARE_SUM_TOLERANCE = 1e-6


def _looks_like_shares(mapping):
    if not mapping:
        return False
    try:
        total = sum(float(v) for v in mapping.values())
    except (TypeError, ValueError):
        return False
    if total <= 0:
        return False
    return abs(total - 1.0) <= SHARE_SUM_TOLERANCE and all(
        float(v) <= 1.0 for v in mapping.values()
    )


def _normalise_value(value):
    """Collapse ints/floats and numeric strings so 5, 5.0 and "5" are one bin."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return str(int(value))
        return repr(float(value))
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def load_samples(path, column=None, key=None):
    """Return a Counter of value -> weight and a description of the input."""
    lowered = path.lower()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        if lowered.endswith(".json"):
            payload = json.load(handle)
            if isinstance(payload, dict) and key and key in payload:
                payload = payload[key]
            if isinstance(payload, dict) and "samples" in payload:
                payload = payload["samples"]
            if isinstance(payload, dict):
                if _looks_like_shares(payload):
                    return (
                        Counter({_normalise_value(k): float(v) for k, v in payload.items()}),
                        "json-shares",
                    )
                return (
                    Counter({_normalise_value(k): float(v) for k, v in payload.items()}),
                    "json-counts",
                )
            if isinstance(payload, list):
                return Counter(_normalise_value(v) for v in payload), "json-list"
            raise SystemExit(f"unsupported JSON shape in {path}")

        if lowered.endswith(".csv"):
            reader = csv.reader(handle)
            rows = [row for row in reader if row and not row[0].startswith("#")]
            if not rows:
                return Counter(), "csv"
            index = -1 if column is None else column
            samples = []
            for row in rows:
                try:
                    samples.append(_normalise_value(row[index]))
                except IndexError:
                    raise SystemExit(f"column {index} missing in CSV row: {row}")
            return Counter(samples), "csv"

        samples = []
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            samples.append(_normalise_value(stripped))
        return Counter(samples), "text"


def analyse(counter, threshold, max_k):
    total = sum(counter.values())
    if total <= 0:
        return {
            "ok": False,
            "error": "no samples",
            "total": 0,
            "distinct": 0,
            "histogram": [],
        }

    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    histogram = [
        {"value": value, "count": count, "share": count / total}
        for value, count in ordered
    ]

    cumulative = 0.0
    min_k = None
    for position, entry in enumerate(histogram, start=1):
        cumulative += entry["share"]
        if min_k is None and cumulative >= threshold:
            min_k = position

    top1 = histogram[0]["share"]
    top_k_share = (
        sum(entry["share"] for entry in histogram[:max_k])
        if max_k > 0
        else 0.0
    )

    if top1 >= threshold:
        recommendation = "dominant_outcome_inline"
        reason = (
            "单一取值覆盖达到阈值，适合把该结果的早退路径内联到调用方，"
            "或把它做成叶子入口"
        )
    elif min_k is not None and min_k <= max_k:
        recommendation = "constant_width_copy"
        reason = (
            f"前 {min_k} 个取值即可覆盖阈值，适合按取值分类并用常量宽度实现"
            "替代通用例程调用"
        )
    else:
        recommendation = "reject"
        reason = (
            f"前 {max_k} 个取值只覆盖 {top_k_share:.3f}，分布不足以支撑特化，"
            "应放弃该候选或扩大取样"
        )

    return {
        "ok": True,
        "total": total,
        "distinct": len(histogram),
        "histogram": histogram,
        "top1_share": top1,
        "threshold": threshold,
        "max_k": max_k,
        "min_k_for_threshold": min_k,
        "top_k_share": top_k_share,
        "dominant": top1 >= threshold,
        "recommendation": recommendation,
        "reason": reason,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="samples file")
    parser.add_argument("--threshold", type=float, default=0.9,
                        help="target coverage for the top-k values (default 0.9)")
    parser.add_argument("--max-k", type=int, default=4,
                        help="largest k worth specialising (default 4)")
    parser.add_argument("--column", type=int, default=None,
                        help="CSV column index (default last)")
    parser.add_argument("--key", default=None,
                        help="JSON object key holding the samples")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    counter, source = load_samples(args.input, args.column, args.key)
    result = analyse(counter, args.threshold, args.max_k)
    result["source"] = source
    result["input"] = args.input

    if args.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0 if result["ok"] else 1

    if not result["ok"]:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    print(f"input={args.input} source={source} total={result['total']} "
          f"distinct={result['distinct']}")
    print(f"{'value':>12s} {'count':>10s} {'share':>8s}")
    for entry in result["histogram"][:12]:
        print(f"{entry['value']:>12s} {entry['count']:>10.0f} {entry['share']:>8.3f}")
    if result["distinct"] > 12:
        print(f"... {result['distinct'] - 12} more distinct values")
    print(
        f"top1_share={result['top1_share']:.3f} "
        f"top{result['max_k']}_share={result['top_k_share']:.3f} "
        f"min_k_for_{result['threshold']}={result['min_k_for_threshold']}"
    )
    print(f"recommendation={result['recommendation']} ({result['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
