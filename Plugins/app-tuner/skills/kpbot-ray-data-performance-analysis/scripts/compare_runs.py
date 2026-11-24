#!/usr/bin/env python3
"""Compare validated metric samples under explicit measurement contracts."""

import argparse
import json
import math
from pathlib import Path
import statistics


CONTEXT_FIELDS = (
    "workload",
    "input_id",
    "scope",
    "resources",
    "software",
    "cache_state",
    "config",
    "correctness_contract",
)


def known(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "unknown", "unspecified")
    if isinstance(value, dict):
        return bool(value) and all(known(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(known(item) for item in value)
    return not isinstance(value, float) or math.isfinite(value)


def validate(document, label):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"{label}: expected schema_version 1 object")
    metric = document.get("metric")
    if (
        not isinstance(metric, dict)
        or not all(
            isinstance(metric.get(key), str) and known(metric[key])
            for key in ("name", "unit", "direction")
        )
        or metric["direction"] not in ("higher", "lower")
    ):
        raise ValueError(f"{label}: invalid metric name/unit/direction")
    if not isinstance(document.get("context"), dict):
        raise ValueError(f"{label}: context must be an object")
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError(f"{label}: samples must be a nonempty array")
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError(f"{label}: sample must be an object")
        value = sample.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                f"{label}: sample value must be finite and positive"
            )


def summary(samples):
    values = [sample["value"] for sample in samples]
    # Normalize before stdev to avoid overflow on large finite measurements.
    scale = max(values)
    normalized = [value / scale for value in values]
    return {
        "n": len(values),
        "values": values,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "cv": (
            statistics.stdev(normalized) / statistics.mean(normalized)
            if len(values) > 1
            else None
        ),
    }


def compare(baseline, candidate, allow_changes=()):
    if set(allow_changes) - {"config", "software"}:
        raise ValueError(
            "Only config and software may be declared experimental factors"
        )
    for label, document in (("baseline", baseline), ("candidate", candidate)):
        validate(document, label)
    reasons, warnings, changes = [], [], {}
    if baseline["metric"] != candidate["metric"]:
        reasons.append("Metric name, unit or direction differs.")
    for label, document in (("baseline", baseline), ("candidate", candidate)):
        for field in CONTEXT_FIELDS:
            if not known(document["context"].get(field)):
                reasons.append(f"{label}: missing or unknown context.{field}")
        for field, value in document["context"].items():
            if field not in CONTEXT_FIELDS and not known(value):
                reasons.append(f"{label}: unknown extra context.{field}")
        for index, sample in enumerate(document["samples"]):
            if (
                sample.get("success") is not True
                or sample.get("correct") is not True
            ):
                reasons.append(
                    f"{label}: sample {index} failed or correctness unverified"
                )
            if not isinstance(sample.get("evidence"), str) or not known(
                sample["evidence"]
            ):
                reasons.append(
                    f"{label}: sample {index} lacks evidence reference"
                )
        if len(document["samples"]) < 3:
            warnings.append(
                f"{label}: fewer than 3 samples; "
                "variability poorly characterized"
            )
    fields = baseline["context"].keys() | candidate["context"].keys()
    for field in sorted(fields):
        before, after = (
            baseline["context"].get(field),
            candidate["context"].get(field),
        )
        if before != after:
            changes[field] = {"baseline": before, "candidate": after}
            if field not in allow_changes:
                reasons.append(
                    f"context.{field} differs "
                    "(not an allowed experimental factor)"
                )
    result = {
        "schema_version": 1,
        "comparable": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "metric": baseline["metric"],
        "changes": changes,
        "baseline": summary(baseline["samples"]),
        "candidate": summary(candidate["samples"]),
        "baseline_samples": baseline["samples"],
        "candidate_samples": candidate["samples"],
        "improvement_percent": None,
        "speedup": None,
        "interpretation": (
            "Descriptive comparison only; no significance or causality claim."
        ),
    }
    if result["comparable"]:
        before, after = (
            result["baseline"]["median"],
            result["candidate"]["median"],
        )
        if baseline["metric"]["direction"] == "higher":
            speedup, improvement = after / before, (after / before - 1) * 100
        else:
            speedup, improvement = before / after, (1 - after / before) * 100
        if math.isfinite(speedup) and math.isfinite(improvement):
            result.update(speedup=speedup, improvement_percent=improvement)
        else:
            result["comparable"] = False
            result["reasons"].append(
                "Metric ratio exceeds finite arithmetic range."
            )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--allow-change",
        action="append",
        default=[],
        choices=("config", "software"),
    )
    args = parser.parse_args()
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        result = compare(baseline, candidate, args.allow_change)
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(
                result, output, ensure_ascii=False, indent=2, allow_nan=False
            )
            output.write("\n")
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(1, f"Cannot compare records: {error}\n")
    raise SystemExit(0 if result["comparable"] else 2)


if __name__ == "__main__":
    main()
