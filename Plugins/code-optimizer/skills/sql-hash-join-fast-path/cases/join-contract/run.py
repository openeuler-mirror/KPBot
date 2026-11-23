"""Exercise batch-probe and byte-equality contracts on synthetic inputs."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LookupStats:
    lookup_keys: int = 0
    batch_calls: int = 0


def lookup_batch(keys, table, stats):
    if not keys:
        return []
    stats.batch_calls += 1
    stats.lookup_keys += len(keys)
    return [table.get(key, ()) for key in keys]


def probe(rows, table, accept=None, *, build_unique=False):
    stats = LookupStats()
    common = rows[0][1] if rows else None
    eligible = (
        build_unique and len(rows) >= 64 and common is not None and accept is None
        and all(key == common for _, key in rows)
    )
    if eligible:
        matches = lookup_batch([common], table, stats)[0]
        return [(row_id, value) for row_id, _ in rows for value in matches], stats
    found = []
    active = [(row_id, key) for row_id, key in rows if key is not None]
    matches = lookup_batch([key for _, key in active], table, stats)
    for (row_id, _), values in zip(active, matches):
        found.extend((row_id, value) for value in values
                     if accept is None or accept(row_id, value))
    return found, stats


def short_equal(left, right):
    if max(len(left), len(right)) > 12:
        return compare(left, right) == 0, "fallback"
    return (len(left) == len(right) and left[:4] == right[:4]
            and left[4:] == right[4:]), "inline"


def compare(left, right):
    return (left > right) - (left < right)


def main():
    sales = [(i, 101) for i in range(100)]
    dates = {101: ("May 1",), 102: ("May 2",)}
    expected_counts = (1, 1, 100, 99, 100, 100, 63, 1, 0)
    scenarios = (
        (sales, dates, None),
        (sales, {102: ("May 2",)}, None),
        (sales[:-1] + [(99, 102)], dates, None),
        (sales[:-1] + [(99, None)], dates, None),
        (sales, {101: ("first", "second")}, None),
        (sales, dates, lambda row, _: row % 2 == 0),
        (sales[:63], dates, None),
        (sales[:64], dates, None),
        ([], dates, None),
    )
    observed_counts = []
    for rows, table, predicate in scenarios:
        baseline, _ = probe(rows, table, predicate)
        build_unique = all(len(matches) == 1 for matches in table.values())
        candidate, stats = probe(rows, table, predicate, build_unique=build_unique)
        assert baseline == candidate
        assert stats.batch_calls == (1 if any(key is not None for _, key in rows) else 0)
        observed_counts.append(stats.lookup_keys)
    assert tuple(observed_counts) == expected_counts
    baseline, baseline_stats = probe(sales, dates)
    shared, shared_stats = probe(sales, dates, build_unique=True)
    assert baseline == shared and len(shared) == 100
    assert (baseline_stats.lookup_keys, shared_stats.lookup_keys) == (100, 1)
    assert (baseline_stats.batch_calls, shared_stats.batch_calls) == (1, 1)
    byte_values = [b"", b"A12", b"A13", b"A12\x00", b"x" * 12, b"x" * 13,
                   "短键".encode(), "短码".encode(), b"abcdX", b"abcdY"]
    for a in byte_values:
        for b in byte_values:
            equal, route = short_equal(a, b)
            assert equal == (compare(a, b) == 0)
            assert route == ("inline" if max(len(a), len(b)) <= 12 else "fallback")
    assert short_equal(b"abcdX", b"abcdY") == (False, "inline")

    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": True, "eligible_probes": observed_counts[0],
                               "byte_comparisons": len(byte_values)**2,
                               "baseline_lookup_keys": baseline_stats.lookup_keys,
                               "shared_lookup_keys": shared_stats.lookup_keys,
                               "baseline_batch_calls": baseline_stats.batch_calls,
                               "shared_batch_calls": shared_stats.batch_calls,
                               "same_prefix_suffix_difference": True}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
