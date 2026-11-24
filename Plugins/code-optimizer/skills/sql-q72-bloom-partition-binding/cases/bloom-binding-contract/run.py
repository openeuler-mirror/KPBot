"""Typed expression/binding model; no native parser, compiler or Bloom runs."""
from copy import deepcopy
import json
from pathlib import Path


FIELD = "cs_sold_date_sk"


def field(name=FIELD, bits=32, scan_id="sales", partition=True):
    return {"op": "field", "name": name, "bits": bits, "scan_id": scan_id, "partition": partition}


def bloom(value=None, seed=42):
    return {"op": "native.might_contain", "blob": b"fixture-bloom", "hash": {
        "op": "native.xxhash64_with_seed", "value": field() if value is None else value, "seed": seed}}


def bind(expression, scan_id="sales", execution_id="run-1", compiled_verifier=lambda node, info: True):
    if expression.get("op") == "and":
        for child in expression["args"]:
            found = bind(child, scan_id, execution_id, compiled_verifier)
            if found is not None:
                return found
        return None
    if expression.get("op") != "native.might_contain" or not isinstance(expression.get("blob"), bytes) or not expression["blob"]:
        return None
    hashed = expression.get("hash", {})
    if hashed.get("op") != "native.xxhash64_with_seed" or type(hashed.get("seed")) is not int:
        return None
    if not -(1 << 63) <= hashed["seed"] < (1 << 63):
        return None
    value = hashed.get("value", {})
    converted = value.get("op") == "cast"
    if converted:
        if value.get("bits") != 64 or value.get("value", {}).get("bits") != 32:
            return None
        value = value["value"]
    if (value.get("op") != "field" or value.get("bits") not in (32, 64)
            or value.get("scan_id") != scan_id or value.get("partition") is not True):
        return None
    info = {"scan_id": scan_id, "field": value["name"], "source_bits": value["bits"],
            "hash_bits": 64, "seed": hashed["seed"], "bloom": expression["blob"],
            "execution_id": execution_id, "expression": expression}
    # Target code must inspect compiled function identity/field binding again.
    return info if compiled_verifier(expression, info) is True else None


def main():
    candidate = bloom()
    bound = bind(candidate)
    widened = bind(bloom({"op": "cast", "bits": 64, "value": field()}))
    nested = {"op": "and", "args": [{"op": "unknown"}, {"op": "and", "args": [candidate]}]}
    rejected = [
        {"op": "or", "args": [candidate, {"op": "other"}]},
        {"op": "not", "args": [candidate]}, bloom(field(partition=False)),
        bloom(field(scan_id="inventory")), bloom(field(bits=16)),
        bloom({"op": "cast", "bits": 32, "value": field(bits=64)}),
        bloom({"op": "udf", "value": field()}), bloom(seed=True), bloom(seed=1 << 63),
    ]
    for replacement in ({"op": "udf"}, {"blob": b""}, {"blob": None},
                        {"hash": {"op": "random", "value": field(), "seed": 42}}):
        changed = deepcopy(candidate)
        changed.update(replacement)
        rejected.append(changed)
    # A Bloom under OR remains ineligible even when that OR is under a top AND.
    hidden_or = {"op": "and", "args": [{"op": "or", "args": [candidate, {"op": "other"}]}]}
    checks = {
        "bloom_binding_route_positive": bound is not None and bound["field"] == FIELD
            and bound["source_bits"] == 32 and bound["hash_bits"] == 64,
        "required_and_only": bind(nested) is not None and bind(hidden_or) is None,
        "unsafe_expressions_rejected": all(bind(item) is None for item in rejected),
        "widening_preserves_original_type": widened is not None and widened["source_bits"] == 32
            and bind(bloom(field(bits=64)))["source_bits"] == 64,
        "compiled_binding_rechecked": bind(candidate, compiled_verifier=lambda node, info: False) is None,
        "original_probe_and_lifetime_retained": bound is not None and bound["expression"] is candidate
            and bound["bloom"] == b"fixture-bloom" and bound["seed"] == 42
            and bound["scan_id"] == "sales" and bound["execution_id"] == "run-1",
    }
    result = {"success": all(checks.values()), **checks}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
