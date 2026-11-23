"""Check reuse, level identity and the arithmetic guard in a synthetic model."""

from collections import defaultdict
import json
from pathlib import Path


def mix(seed, field):
    code = 0 if field is None else field + 1
    return (31 * seed + code) % 2**32


def aggregate(batches, reuse=False):
    sums = defaultdict(int)
    hashes = []
    evaluations = 0
    for batch in batches:
        prepared = []
        if reuse:
            for category, product, cents, count in batch:
                prepared.append((cents * count if cents is not None else 0, mix(17, category)))
                evaluations += 1
        for level in (2, 1, 0):
            for i, (category, product, cents, count) in enumerate(batch):
                keys = (category, product)[:level]
                if reuse:
                    amount, prefix = prepared[i]
                    h = prefix if level else 17
                    fields = keys[1:] if level else ()
                else:
                    amount = cents * count if cents is not None else 0
                    evaluations += 1
                    h, fields = 17, keys
                for field in fields:
                    h = mix(h, field)
                hashes.append(mix(h, level))
                projected = keys + (None,) * (2 - level)
                sums[(level, projected)] += amount
    return dict(sums), hashes, evaluations


def checked_add(total, value):
    result = total + value
    return result if -(2**127) <= result < 2**127 else None


def candidate_add(total, value):
    if -(2**63) <= value < 2**63 and total >> 64 in (0, -1):
        return total + value, "direct"
    return checked_add(total, value), "fallback"


def main():
    batches = (
        ((1, 10, 1000, 2), (1, 10, 1000, 1), (2, 20, 500, 1), (None, 10, None, 1)),
        ((2, None, -500, 1), (1, 10, 300, 1), (None, None, 200, 1)),
    )
    old_sums, old_hashes, old_evaluations = aggregate(batches)
    sums, hashes, evaluations = aggregate(batches, reuse=True)
    assert sums == old_sums and hashes == old_hashes
    assert sums[(2, (1, 10))] == 3300
    # Actual NULL data, a subtotal, and the grand total project the same keys.
    assert [sums[(level, (None, None))] for level in (2, 1, 0)] == [200, 200, 3500]
    assert (old_evaluations, evaluations) == (21, 7)
    totals = (0, -1, 2**64-1, -2**64, 2**64, 2**127-1, -(2**127))
    values = (-(2**127), -(2**63)-1, -(2**63), -1, 0, 1, 2**63-1, 2**63, 2**127-1)
    routes = set()
    for total in totals:
        for value in values:
            result, route = candidate_add(total, value)
            assert result == checked_add(total, value)
            if not -(2**63) <= value < 2**63:
                assert route == "fallback"
            routes.add(route)
    assert routes == {"direct", "fallback"}
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": True, "amount_evaluations": evaluations,
                               "baseline_amount_evaluations": old_evaluations,
                               "arithmetic_checks": len(totals)*len(values)}) + "\n",
                   encoding="utf-8")


if __name__ == "__main__":
    main()
