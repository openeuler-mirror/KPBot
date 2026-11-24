"""Check reuse, level identity and the arithmetic guard in a synthetic model."""

from collections import Counter, defaultdict
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


class AmountCache:
    """Keep one cache across calls; mutable vectors require explicit generations."""

    def __init__(self):
        self.key = None
        self.vector = None
        self.amounts = ()
        self.hits = 0
        self.misses = 0
        self.evaluations = 0

    @staticmethod
    def context_key(vector, batch_id, revision, selection, mapping):
        return id(vector), batch_id, revision, tuple(selection), tuple(mapping)

    def get(self, vector, batch_id, revision, selection, mapping):
        key = self.context_key(vector, batch_id, revision, selection, mapping)
        if key == self.key:
            self.hits += 1
            return self.amounts
        result = []
        for row in selection:
            _, _, cents, count = vector[mapping[row]]
            result.append(cents * count if cents is not None else 0)
        self.key, self.vector, self.amounts = key, vector, tuple(result)
        self.misses += 1
        self.evaluations += len(result)
        return self.amounts


def cache_invalidation_checks():
    vector = [(1, 10, 100, 2), (2, 20, 300, 1)]
    identity = id(vector)
    cache = AmountCache()
    assert cache.get(vector, 1, 0, (0, 1), (0, 1)) == (200, 300)
    assert cache.get(vector, 1, 0, (0, 1), (0, 1)) == (200, 300)
    # Mutate the same vector within a batch, publishing a new revision.
    vector[0] = (1, 10, 500, 2)
    assert cache.get(vector, 1, 1, (0, 1), (0, 1)) == (1000, 300)
    # Reuse that very object for the next batch; the batch generation changes.
    vector[0] = (1, 10, 700, 2)
    assert cache.get(vector, 2, 1, (0, 1), (0, 1)) == (1400, 300)
    assert cache.get(vector, 2, 1, (1,), (0, 1)) == (300,)
    assert cache.get(vector, 2, 1, (1,), (1, 0)) == (1400,)
    assert id(vector) == identity and cache.vector is vector
    assert (cache.hits, cache.misses, cache.evaluations) == (1, 5, 8)
    return {"cache_hits": cache.hits, "cache_invalidations": cache.misses - 1,
            "cache_amount_evaluations": cache.evaluations}


def grouping_instances(rows, instances, *, reuse_amount=False, include_instance=True):
    """Model duplicate GROUP BY ALL GROUPING SETS ((a), (a)) instances."""
    sums = defaultdict(int)
    prepared = [amount for _, amount in rows] if reuse_amount else None
    evaluations = len(rows) if reuse_amount else 0
    for instance_id, grouping_mask in instances:
        for i, (key, amount) in enumerate(rows):
            if reuse_amount:
                amount = prepared[i]
            else:
                evaluations += 1
            identity = (instance_id, key, grouping_mask) if include_instance else (key, grouping_mask)
            sums[identity] += amount
    # Instance identity is internal: output still preserves duplicate rows.
    return [(*identity[-2:], amount) for identity, amount in sums.items()], evaluations


def duplicate_grouping_set_checks():
    rows = [(7, 10)]
    instances = [(0, 0), (1, 0)]  # Same key set and GROUPING(a) mask, distinct instances.
    baseline, before = grouping_instances(rows, instances)
    candidate, after = grouping_instances(rows, instances, reuse_amount=True)
    assert Counter(candidate) == Counter(baseline) == Counter({(7, 0, 10): 2})
    assert (before, after) == (2, 1)
    unsafe, _ = grouping_instances(rows, instances, reuse_amount=True, include_instance=False)
    assert unsafe == [(7, 0, 20)] and Counter(unsafe) != Counter(baseline)
    return {"duplicate_grouping_rows": len(candidate), "duplicate_grouping_each_amount": 10,
            "duplicate_grouping_amount_evaluations": after}


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
    result = {"success": True, "amount_evaluations": evaluations,
              "baseline_amount_evaluations": old_evaluations,
              "arithmetic_checks": len(totals)*len(values)}
    result.update(cache_invalidation_checks())
    result.update(duplicate_grouping_set_checks())
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
