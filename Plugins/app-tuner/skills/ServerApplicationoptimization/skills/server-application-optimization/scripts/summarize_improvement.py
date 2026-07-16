#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text())


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def higher_is_better(metric_name: str) -> bool:
    name = metric_name.lower()
    lower_better_tokens = [
        "latency",
        "delay",
        "response_time",
        "rt",
        "p50",
        "p90",
        "p95",
        "p99",
        "p999",
        "error",
        "fail",
    ]
    return not any(token in name for token in lower_better_tokens)


def main():
    parser = argparse.ArgumentParser(description="Summarize optimization improvement.")
    parser.add_argument("--baseline", required=True, help="Path to baseline JSON file")
    parser.add_argument("--candidate", required=True, help="Path to tuned JSON file")
    parser.add_argument("--round-name", default="current-round", help="Label for this cumulative validation round")
    args = parser.parse_args()

    baseline = load_json(args.baseline)
    candidate = load_json(args.candidate)

    summary = {}
    for key, before_value in baseline.items():
        before = to_float(before_value)
        after = to_float(candidate.get(key))
        if before is None or after is None or before == 0:
            summary[key] = {
                "before": before_value,
                "after": candidate.get(key),
                "improvement_percent": "n/a",
            }
            continue

        if higher_is_better(key):
            improvement = ((after - before) / before) * 100.0
        else:
            improvement = ((before - after) / before) * 100.0
        summary[key] = {
            "before": before,
            "after": after,
            "improvement_percent": round(improvement, 2),
            "direction": "higher_is_better" if higher_is_better(key) else "lower_is_better",
        }

    result = {
        "round_name": args.round_name,
        "validation_model": "cumulative_serial",
        "summary": summary,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
