"""Reject skipped or incomplete legacy AgentENV snapshot benchmark output."""

import argparse
import json
import math
import re
from pathlib import Path


FAILURE_PATTERNS = (
    r"(?m)^\s*Skipping\b",
    r"(?m)^\s*No snapshot benchmarks matched\b",
    r"(?m)^\s*snapshot benchmark failed:",
    r"(?m)^thread\b.*\bpanicked\b",
    r"(?m)^Error:",
)


def inspect_log(text: str, benchmark: str, expected: int, exit_code: int) -> dict:
    """Check artifact usability, not correctness of the workload or performance gain."""
    result = {
        "scope": "legacy_snapshot_log_validation",
        "artifact_status": "rejected",
        "benchmark": benchmark,
        "expected_samples": expected,
        "samples_ms": [],
        "gain_pct": None,
        "issues": [],
    }
    if exit_code != 0:
        result["issues"].append(f"Process exit code is {exit_code}")
    for pattern in FAILURE_PATTERNS:
        match = re.search(pattern, text)
        if match:
            result["issues"].append(f"Failure/skip marker: {match.group(0).strip()}")
    pattern = r"(?m)^" + re.escape(benchmark) + r"\s+samples\s+\[([^\]\n]*)\]\s*$"
    sample_lines = re.findall(pattern, text)
    if len(sample_lines) != 1:
        result["issues"].append("Expected exactly one raw sample line for this benchmark")
    else:
        for item in sample_lines[0].split(","):
            match = re.fullmatch(r"\s*([+-]?[0-9.eE+-]+)\s+ms\s*", item)
            if match is None:
                result["issues"].append(f"Invalid sample or unsupported unit: {item.strip()}")
                continue
            try:
                value = float(match.group(1))
            except ValueError:
                result["issues"].append(f"Invalid numeric sample: {item.strip()}")
                continue
            if not math.isfinite(value) or value < 0:
                result["issues"].append("Samples must be finite and non-negative")
                continue
            result["samples_ms"].append(value)
        if len(result["samples_ms"]) != expected:
            result["issues"].append(
                f"Expected {expected} samples; found {len(result['samples_ms'])}"
            )
    if not result["issues"]:
        result["artifact_status"] = "usable"
    return result


def main() -> int:
    """Read an existing log and the actual saved process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--expected-samples", type=int, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    arguments = parser.parse_args()
    if arguments.expected_samples < 1:
        parser.error("--expected-samples must be positive")
    try:
        text = arguments.log.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        result = {"artifact_status": "rejected", "issues": [str(error)], "gain_pct": None}
    else:
        result = inspect_log(
            text, arguments.benchmark, arguments.expected_samples, arguments.exit_code
        )
    print(json.dumps(result, indent=2))
    return 0 if result["artifact_status"] == "usable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
