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


def check_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main():
    sales = [(i, 101) for i in range(100)]
    dates = {101: ("May 1",), 102: ("May 2",)}
    expected_counts = (1, 1, 100, 99, 100, 100, 63, 1, 0, 100)
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
        (sales, {101: (0, 0, 1)}, lambda row, value: row % 2 == value),
    )
    observed_counts = []
    for index, (rows, table, predicate) in enumerate(scenarios):
        # Use a nested-loop inner join as an oracle independent of both probe paths.
        expected_rows = [
            (row_id, value)
            for row_id, key in rows if key is not None
            for build_key, values in table.items() if build_key == key
            for value in values if predicate is None or predicate(row_id, value)
        ]
        baseline, _ = probe(rows, table, predicate)
        build_unique = all(len(matches) == 1 for matches in table.values())
        candidate, stats = probe(rows, table, predicate, build_unique=build_unique)
        check_equal(baseline, expected_rows, f"scenario {index} baseline rows")
        check_equal(candidate, expected_rows, f"scenario {index} candidate rows")
        expected_calls = int(any(key is not None for _, key in rows))
        check_equal(stats.batch_calls, expected_calls, f"scenario {index} batch calls")
        observed_counts.append(stats.lookup_keys)
    check_equal(tuple(observed_counts), expected_counts, "per-scenario lookup key counts")
    _, baseline_stats = probe(sales, dates)
    _, shared_stats = probe(sales, dates, build_unique=True)
    check_equal((baseline_stats.lookup_keys, shared_stats.lookup_keys), (100, 1), "lookup keys")
    check_equal((baseline_stats.batch_calls, shared_stats.batch_calls), (1, 1), "batch calls")
    byte_values = [b"", b"A12", b"A13", b"A12\x00", b"x" * 12, b"x" * 13,
                   "短键".encode(), "短码".encode(), b"abcdX", b"abcdY"]
    for a in byte_values:
        for b in byte_values:
            equal, route = short_equal(a, b)
            check_equal(equal, a == b, f"byte equality {a!r}, {b!r}")
            expected_route = "inline" if max(len(a), len(b)) <= 12 else "fallback"
            check_equal(route, expected_route, f"comparison route {a!r}, {b!r}")
    suffix_result = short_equal(b"abcdX", b"abcdY")
    check_equal(suffix_result, (False, "inline"), "same-prefix suffix difference")

    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": True, "eligible_probes": observed_counts[0],
                               "byte_comparisons": len(byte_values)**2,
                               "baseline_lookup_keys": baseline_stats.lookup_keys,
                               "shared_lookup_keys": shared_stats.lookup_keys,
                               "baseline_batch_calls": baseline_stats.batch_calls,
                               "shared_batch_calls": shared_stats.batch_calls,
                               "same_prefix_suffix_difference": not suffix_result[0]}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
