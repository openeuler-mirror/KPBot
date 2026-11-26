"""Submission/evaluation model with a bound, injected tri-state probe."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re


FIELD = "cs_sold_date_sk"


@dataclass(frozen=True)
class Split:
    name: str
    keys: dict
    scan_id: str = "sales"
    format: str = "parquet"
    plain_hive: bool = True
    path: str = "/data/input.parquet"
    start: int = 10
    length: int = 100
    properties: tuple = (("encryption", "unchanged"),)


def fixture_binding():
    # Models the descriptor from sql-q72-bloom-partition-binding, not a parser.
    return {"scan_id": "sales", "field": FIELD, "source_bits": 32, "hash_bits": 64,
            "seed": 42, "bloom": b"fixture-bloom", "expression": "bound-native-probe",
            "execution_id": "run-1"}


def route(split, binding, evaluator, execution_id="run-1"):
    if (binding is None or split.format != "parquet" or not split.plain_hive
            or binding["scan_id"] != split.scan_id or binding["execution_id"] != execution_id
            or binding["source_bits"] not in (32, 64)):
        return "fallback"
    value = split.keys.get(binding["field"])
    if value is None or not isinstance(value, str) or re.fullmatch(r"[+-]?[0-9]{1,20}", value) is None:
        return "fallback"
    integer = int(value)
    limit = 1 << (binding["source_bits"] - 1)
    if not -limit <= integer < limit:
        return "fallback"
    try:
        result = evaluator(integer)
    except (ValueError, RuntimeError, OverflowError):
        return "fallback"
    return "skip" if result is False else "submit"


def submit(splits, binding, evaluator, execution_id="run-1"):
    kept, decisions = [], {}
    for split in splits:
        decision = route(split, binding, evaluator, execution_id)
        decisions[split.name] = decision
        if decision != "skip":
            kept.append(split)
    return kept, decisions


def fixture_probe(value):
    if value == 103:
        raise ValueError("fixture evaluation failure")
    return {99: False, 100: None, 101: True, 102: True}.get(value)


def raise_error(error):
    def evaluate(value):
        raise error("fixture evaluator error")
    return evaluate


def recoverable_errors_fall_back(split, binding):
    for error in (ValueError, RuntimeError, OverflowError):
        try:
            if route(split, binding, raise_error(error)) != "fallback":
                return False
        except (ValueError, RuntimeError, OverflowError):
            return False
    return True


def main():
    binding = fixture_binding()
    values = {"s99": "99", "s100": "100", "s101": "101", "s102": "102", "s103": "103",
              "null": None, "invalid": "abc", "overflow": str(1 << 31)}
    splits = [Split(name, {FIELD: value}) for name, value in values.items()]
    kept, decisions = submit(splits, binding, fixture_probe)
    opened = [split.name for split in kept]
    base = Split("example", {FIELD: "99"})
    unsupported = (replace(base, format="orc"), replace(base, plain_hive=False),
                   replace(base, scan_id="other"), replace(base, keys={"other": "99"}))
    wide_binding = dict(binding, source_bits=64)
    fatal_propagates = False
    try:
        route(base, binding, raise_error(MemoryError))
    except MemoryError:
        fatal_propagates = True
    all_pruned, all_pruned_decisions = submit([base], binding, lambda value: False)
    checks = {
        "split_prune_route_positive": decisions["s99"] == "skip" and "s99" not in opened,
        "true_and_false_positive_still_submit": decisions["s101"] == decisions["s102"] == "submit"
            and [name for name in opened if name == "s102"] == ["s102"],
        "null_and_errors_do_not_prune": decisions["s100"] == "submit" and all(
            decisions[name] == "fallback" for name in ("s103", "null", "invalid", "overflow")),
        "unsupported_split_falls_back": all(route(item, binding, fixture_probe) == "fallback" for item in unsupported)
            and route(base, None, fixture_probe) == "fallback",
        "binding_lifecycle_checked": route(base, binding, fixture_probe, execution_id="run-2") == "fallback",
        "retained_split_identity_and_metadata": len(kept) == 7 and all(
            any(item is original for original in splits) and item.start == 10 and item.length == 100
            and item.properties == (("encryption", "unchanged"),) for item in kept),
        "signed_partition_range": route(replace(base, keys={FIELD: str(-(1 << 31))}), binding, lambda n: n != -(1 << 31)) == "skip"
            and route(replace(base, keys={FIELD: str(1 << 31)}), wide_binding, lambda n: n != 1 << 31) == "skip"
            and route(replace(base, keys={FIELD: str(1 << 63)}), wide_binding, lambda n: False) == "fallback",
        "recoverable_evaluation_errors_fall_back": recoverable_errors_fall_back(base, binding),
        "fatal_resource_error_propagates": fatal_propagates,
        "all_pruned_returns_empty_submission": all_pruned == [] and all_pruned_decisions == {base.name: "skip"}
            and submit([], binding, fixture_probe) == ([], {}),
    }
    result = {"success": all(checks.values()), **checks}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
