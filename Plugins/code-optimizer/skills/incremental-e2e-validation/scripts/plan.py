"""Generate comparison identities and descriptive paired gains."""
import argparse
import json
import math
import statistics


def plan(data):
    items = data["optimizations"]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("optimizations must be a list of objects")
    ids = [item["id"] for item in items]
    if not ids or any(not isinstance(x, str) or not x.strip() for x in ids):
        raise ValueError("Provide nonempty optimization IDs")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate optimization ID")
    for item in items:
        for field in ("requires", "excludes"):
            refs = item.get(field, [])
            if not isinstance(refs, list) or any(x not in ids or x == item["id"] for x in refs):
                raise ValueError("Invalid dependency or exclusion")
    configs, pairs, rows = [], [], []
    keys = {}

    def config(active):
        key = tuple(active)
        if key in keys:
            return keys[key]
        reasons = []
        for item in items:
            if item["id"] in active:
                missing = set(item.get("requires", [])) - set(active)
                conflict = set(item.get("excludes", [])) & set(active)
                if missing:
                    reasons.append(f"{item['id']} requires {sorted(missing)}")
                if conflict:
                    reasons.append(f"{item['id']} excludes {sorted(conflict)}")
        name = f"c{len(configs)}"
        keys[key] = name
        configs.append({"id": name, "active": list(active), "reasons": reasons})
        return name

    def pair(left, right):
        value = {"control": left, "candidate": right}
        if value not in pairs:
            pairs.append(value)
        return pairs.index(value)

    baseline = config([])
    previous = baseline
    for index, name in enumerate(ids):
        single = config([name])
        cumulative = config(ids[:index + 1])
        rows.append({"optimization": name,
                     "independent": pair(baseline, single),
                     "incremental": pair(previous, cumulative),
                     "cumulative": pair(baseline, cumulative)})
        previous = cumulative
    return {"configurations": configs, "comparisons": pairs, "rows": rows,
            "all_combined": rows[-1]["cumulative"]}


def summarize(data):
    direction = data["direction"]
    if direction not in ("lower", "higher"):
        raise ValueError("direction must be lower or higher")
    blocks = data["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Provide paired blocks")
    gains = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Each block must be an object")
        x, y = block["control"], block["candidate"]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (x, y)):
            raise ValueError("Metrics must be finite positive numbers")
        gain = (x - y) / x * 100 if direction == "lower" else (y - x) / x * 100
        if not math.isfinite(gain):
            raise ValueError("Nonfinite relative change")
        gains.append(gain)
    return {"gain_pct": statistics.median(gains), "blocks": len(gains),
            "range_pct": [min(gains), max(gains)], "per_block_pct": gains,
            "verdict": "requires_evidence_review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    try:
        with open(args.input, encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Input must be an object")
        result = summarize(data) if args.summarize else plan(data)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        parser.exit(2, f"Invalid input: {error}\n")


if __name__ == "__main__":
    main()
