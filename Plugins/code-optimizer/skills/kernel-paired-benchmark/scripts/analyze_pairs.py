"""Recompute one workload/process from four-leg paired timing records."""

import argparse
import json
import math
import sys


def positive_number(value):
    if type(value) not in (int, float) or value <= 0:
        raise ValueError("Expected a finite positive number")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise ValueError("Number is outside the supported float range") from error
    if not finite:
        raise ValueError("Expected a finite positive number")
    return value


def quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def analyze(payload):
    if not isinstance(payload, dict):
        raise ValueError("Measurement must be an object")
    for field in ("experiment_id", "workload_id", "process_run_id"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"Missing identity: {field}")
    limits = payload.get("controls")
    if not isinstance(limits, dict):
        raise ValueError("Missing predefined control limits")
    for field in ("median_min", "median_max", "p10_min", "p90_max"):
        positive_number(limits.get(field))
    if not (limits["p10_min"] <= limits["median_min"] <= 1
            <= limits["median_max"] <= limits["p90_max"]):
        raise ValueError("Control limits must be ordered around one")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Missing timing blocks")
    ratios = {"AA": [], "BB": [], "AB": []}
    order_counts = {name: {"ABBA": 0, "BAAB": 0} for name in ratios}
    expected_blocks = payload.get("expected_blocks")
    if "expected_blocks" in payload:
        if not isinstance(expected_blocks, dict) or set(expected_blocks) != set(ratios):
            raise ValueError("Expected block counts must specify AA, BB and AB")
        if any(type(count) is not int or count <= 0
               for count in expected_blocks.values()):
            raise ValueError("Expected block counts must be positive integers")
    identifiers = set()
    iteration_counts = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Block must be an object")
        for field in ("experiment_id", "workload_id", "process_run_id"):
            if field in block and block[field] != payload[field]:
                raise ValueError(f"Mixed block identity: {field}")
        experiment = block.get("experiment")
        block_id = block.get("block_id")
        order = block.get("order")
        if experiment not in ratios or type(block_id) is not int or block_id < 0:
            raise ValueError("Invalid experiment or block ID")
        if (experiment, block_id) in identifiers:
            raise ValueError("Duplicate block")
        identifiers.add((experiment, block_id))
        legs = block.get("legs")
        if (order not in ("ABBA", "BAAB") or not isinstance(legs, list)
                or len(legs) != 4):
            raise ValueError("Expected four legs in ABBA or BAAB order")
        totals = {"A": 0, "B": 0}
        for index, leg in enumerate(legs, 1):
            if not isinstance(leg, dict):
                raise ValueError("Leg must be an object")
            for field in ("experiment_id", "workload_id", "process_run_id"):
                if field in leg and leg[field] != payload[field]:
                    raise ValueError(f"Mixed leg identity: {field}")
            label = order[index - 1]
            if experiment == "AA":
                implementation = "original"
            elif experiment == "BB":
                implementation = "candidate"
            else:
                implementation = "original" if label == "A" else "candidate"
            if (leg.get("leg_index") != index or type(leg.get("leg_index")) is not int
                    or leg.get("label") != label
                    or leg.get("implementation") != implementation):
                raise ValueError("Leg order, label or implementation mismatch")
            iterations = leg.get("iterations")
            elapsed = leg.get("elapsed_ns")
            if type(iterations) is not int or iterations <= 0:
                raise ValueError("Iterations must be positive integers")
            if type(elapsed) is not int or elapsed <= 0:
                raise ValueError("Elapsed nanoseconds must be positive integers")
            iteration_counts.add(iterations)
            totals[label] += elapsed
        try:
            ratio = totals["A"] / totals["B"]
        except OverflowError as error:
            raise ValueError("Paired ratio exceeds the supported float range") from error
        positive_number(ratio)
        ratios[experiment].append(ratio)
        order_counts[experiment][order] += 1
    if len(iteration_counts) != 1 or any(not values for values in ratios.values()):
        raise ValueError("Expected AA/BB/AB with a common iteration count")
    if expected_blocks is not None:
        for experiment, expected in expected_blocks.items():
            if len(ratios[experiment]) != expected:
                raise ValueError(f"Block count mismatch for {experiment}")
    summaries = {}
    for experiment, values in ratios.items():
        summaries[experiment] = {
            "blocks": len(values), "ratios": values,
            "order_counts": order_counts[experiment],
            "median": quantile(values, 0.5),
            "p10": quantile(values, 0.1), "p90": quantile(values, 0.9),
        }
    controls_ok = all(
        limits["median_min"] <= summaries[name]["median"] <= limits["median_max"]
        and summaries[name]["p10"] >= limits["p10_min"]
        and summaries[name]["p90"] <= limits["p90_max"]
        for name in ("AA", "BB")
    )
    orders_balanced = all(
        abs(counts["ABBA"] - counts["BAAB"]) <= 1
        for counts in order_counts.values()
    )
    reasons = []
    if not controls_ok:
        reasons.append("self_controls_failed")
    if not orders_balanced:
        reasons.append("unbalanced_orders")
    return {"experiment_id": payload["experiment_id"],
            "workload_id": payload["workload_id"],
            "process_run_id": payload["process_run_id"],
            "completeness_status": "complete" if expected_blocks else "not_declared",
            "measurement_status": "inconclusive" if reasons else "controls_passed",
            "inconclusive_reasons": reasons,
            "experiments": summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8-sig") as stream:
            result = analyze(json.load(stream))
        print(json.dumps(result, allow_nan=False))
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
