#!/usr/bin/env python3
"""Read-only ABBA block comparison of cube-bench create total time (stdlib)."""

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
from pathlib import Path


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=unique_object)


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{label}: non-finite or non-positive value")
    return float(value)


def integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}: expected an integer")
    return value


def required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: expected non-empty text")
    return value


def workload(config):
    if not isinstance(config, dict):
        raise ValueError("missing report config/workload")
    template = config.get("template", config.get("template_id"))
    if "template" in config and "template_id" in config:
        if config["template"] != config["template_id"]:
            raise ValueError("conflicting template fields")
    return {
        "concurrency": integer(config["concurrency"], "concurrency"),
        "total": integer(config["total"], "total"),
        "mode": required_text(config["mode"], "mode"),
        "template": required_text(template, "template"),
    }


def percentile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    low = math.floor(index)
    high = math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    if integer(manifest["schema_version"], "schema_version") != 1:
        raise ValueError("unsupported schema_version")
    metric = manifest["metric"]
    factors = {"summary.total_time_ms": 1, "summary.total_time_s": 1000}
    if metric not in factors:
        raise ValueError("metric must explicitly select total_time_ms or total_time_s")
    threshold = number(manifest["minimum_reduction_pct"], "minimum_reduction_pct")
    if not 0 <= threshold < 100:
        raise ValueError("minimum_reduction_pct must be in [0, 100)")
    expected = workload(manifest["workload"])
    if (expected["concurrency"], expected["total"], expected["mode"]) != (100, 100, "create-only"):
        raise ValueError("this protocol requires c100/n100 create-only")
    environment = required_text(manifest["environment_id"], "environment_id")
    variants = manifest["variants"]
    if set(variants) != {"base", "candidate"}:
        raise ValueError("exactly base and candidate variants required")
    for name, variant in variants.items():
        required_text(variant["artifact_id"], name + ".artifact_id")
        if variant["correctness_passed"] is not True:
            raise ValueError(f"{name}: correctness has not passed")
    runs = manifest["runs"]
    if not isinstance(runs, list) or len(runs) < 48 or len(runs) % 4:
        raise ValueError("at least 12 complete ABBA blocks required")
    seen = set()
    identities = []
    slots = []
    for index, run in enumerate(runs):
        block = integer(run["block"], "block")
        position = integer(run["position"], "position")
        if (block, position) != divmod(index, 4):
            raise ValueError("runs must be in consecutive chronological block/position order")
        if run["variant"] != ("base", "candidate", "candidate", "base")[position]:
            raise ValueError("invalid ABBA assignment")
        if run["environment_id"] != environment:
            raise ValueError("environment drift within comparison")
        reports = run["reports"]
        if not isinstance(reports, list) or len(reports) != 5:
            raise ValueError("each position requires exactly 5 independent reports")
        samples = []
        for filename in reports:
            path = (manifest_path.parent / required_text(filename, "report path")).resolve()
            if path in seen:
                raise ValueError(f"report reused: {path}")
            seen.add(path)
            report = read_json(path)
            if workload(report["config"]) != expected:
                raise ValueError(f"workload mismatch: {path}")
            summary = report["summary"]
            if integer(summary["successful"], "successful") != expected["total"]:
                raise ValueError(f"incomplete or failed creates: {path}")
            if integer(summary["errors"], "errors") != 0:
                raise ValueError(f"failed requests: {path}")
            for request in report.get("raw", []):
                if request.get("error"):
                    raise ValueError(f"raw request failure: {path}")
            field = metric.split(".")[1]
            value = number(summary[field], field, positive=True) * factors[metric]
            if "total_time_ms" in summary and "total_time_s" in summary:
                ms = number(summary["total_time_ms"], "total_time_ms", positive=True)
                seconds = number(summary["total_time_s"], "total_time_s", positive=True)
                if not math.isclose(ms, seconds * 1000, rel_tol=1e-6, abs_tol=0.001):
                    raise ValueError(f"conflicting time units: {path}")
            samples.append(value)
            identities.append({"path": str(path), "sha256": digest(path), "total_time_ms": value})
        slots.append(statistics.median(samples))
    blocks = []
    for offset in range(0, len(slots), 4):
        a = statistics.median((slots[offset], slots[offset + 3]))
        b = statistics.median((slots[offset + 1], slots[offset + 2]))
        blocks.append({"block": offset // 4, "base_ms": a, "candidate_ms": b,
                       "reduction_pct": 100 * (a - b) / a})
    effects = [block["reduction_pct"] for block in blocks]
    rng = random.Random(0)
    bootstrap = [statistics.median(rng.choices(effects, k=len(effects))) for _ in range(10000)]
    interval = [percentile(bootstrap, 0.025), percentile(bootstrap, 0.975)]
    wins = sum(effect > 0 for effect in effects)
    losses = sum(effect < 0 for effect in effects)
    n = wins + losses
    p_value = min(1.0, 2 * sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n) if n else 1.0
    verdict = "inconclusive"
    if interval[0] > threshold and p_value <= 0.05:
        verdict = "improved"
    elif interval[1] < 0 and p_value <= 0.05:
        verdict = "regressed"
    return {
        "success": True, "verdict": verdict, "method": "abba-block-median-v1",
        "metric": metric, "normalized_unit": "ms", "workload": expected,
        "environment_id": environment, "variants": variants,
        "manifest_sha256": digest(manifest_path), "minimum_reduction_pct": threshold,
        "reduction_pct": statistics.median(effects), "bootstrap_95ci_pct": interval,
        "bootstrap_resamples": 10000, "bootstrap_seed": 0,
        "sign_test_two_sided_p": p_value, "wins": wins, "losses": losses,
        "ties": len(effects) - n, "blocks": blocks, "reports": identities,
        "limitations": ["Environment and correctness metadata are operator attestations.",
                        "Statistical comparison does not verify durability or causal attribution.",
                        "CI assumes independent comparable blocks; no multiplicity correction."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="new output file; existing files are never overwritten")
    args = parser.parse_args()
    try:
        result = compare(args.manifest)
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload)
        else:
            sys.stdout.write(payload)
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
