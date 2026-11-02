#!/usr/bin/env python3
"""Compare vector-search baseline and candidate runs from a small JSON schema."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any


def median_number(rows: list[dict[str, Any]], field: str) -> float:
    values = []
    for index, row in enumerate(rows):
        value = row.get(field)
        if type(value) not in (int, float):
            raise ValueError(f"sample {index} field {field!r} must be a finite number")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"sample {index} field {field!r} is outside numeric range") from exc
        if not math.isfinite(number):
            raise ValueError(f"sample {index} field {field!r} must be finite")
        values.append(number)
    if not values:
        raise ValueError(f"field {field!r} has no numeric samples")
    median = float(statistics.median(values))
    if not math.isfinite(median):
        raise ValueError(f"field {field!r} median is outside numeric range")
    return median


def compare(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    perf_field = payload.get("performance_field")
    if not isinstance(perf_field, str) or not perf_field:
        raise ValueError("performance_field must be a non-empty string")
    higher_is_better = payload.get("higher_is_better", True)
    if not isinstance(higher_is_better, bool):
        raise ValueError("higher_is_better must be boolean")
    correctness_fields = payload.get("correctness_fields", [])
    if not isinstance(correctness_fields, list) or not all(
        isinstance(item, str) and item.strip() for item in correctness_fields
    ):
        raise ValueError("correctness_fields must be an array of strings")
    datasets = payload.get("datasets")
    if not isinstance(datasets, dict) or not datasets:
        raise ValueError("datasets must be a non-empty object")

    output: dict[str, Any] = {"datasets": {}, "geomean_speedup": None}
    speedups: list[float] = []
    for name, entry in datasets.items():
        if not isinstance(entry, dict):
            raise ValueError(f"dataset {name!r} must be an object")
        baseline = entry.get("baseline")
        candidate = entry.get("candidate")
        if not isinstance(baseline, list) or not baseline:
            raise ValueError(f"dataset {name!r} baseline must be non-empty")
        if not isinstance(candidate, list) or not candidate:
            raise ValueError(f"dataset {name!r} candidate must be non-empty")
        if not all(isinstance(row, dict) for row in baseline + candidate):
            raise ValueError(f"dataset {name!r} samples must be objects")

        a = median_number(baseline, perf_field)
        b = median_number(candidate, perf_field)
        if a <= 0 or b <= 0:
            raise ValueError(f"dataset {name!r} performance medians must be positive")
        speedup = b / a if higher_is_better else a / b
        if not math.isfinite(speedup) or speedup <= 0:
            raise ValueError(f"dataset {name!r} speedup is outside numeric range")
        speedups.append(speedup)

        correctness: dict[str, Any] = {}
        for field in correctness_fields:
            try:
                base_value = median_number(baseline, field)
                cand_value = median_number(candidate, field)
            except ValueError:
                correctness[field] = {"available": False}
                continue
            correctness[field] = {
                "available": True,
                "baseline_median": base_value,
                "candidate_median": cand_value,
                "delta": cand_value - base_value,
            }

        output["datasets"][name] = {
            "baseline_median": a,
            "candidate_median": b,
            "speedup": speedup,
            "improvement_percent": (speedup - 1.0) * 100.0,
            "baseline_samples": len(baseline),
            "candidate_samples": len(candidate),
            "correctness": correctness,
        }

    output["geomean_speedup"] = math.exp(
        sum(math.log(value) for value in speedups) / len(speedups)
    )
    output["geomean_improvement_percent"] = (
        output["geomean_speedup"] - 1.0
    ) * 100.0
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input JSON file")
    parser.add_argument("--output", type=Path, help="Optional output JSON file")
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = compare(payload)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
