"""Exercise batch-probe and byte-equality contracts on synthetic inputs."""

import json
from pathlib import Path


def probe(rows, table, accept=None, *, build_unique=False):
    common = rows[0][1] if rows else None
    eligible = (
        build_unique and len(rows) >= 64 and common is not None and accept is None
        and all(key == common for _, key in rows)
    )
    if eligible:
        matches = table.get(common, ())
        return [(row_id, value) for row_id, _ in rows for value in matches], 1
    found = []
    lookups = 0
    for row_id, key in rows:
        if key is not None:
            lookups += 1
            found.extend(
                (row_id, value) for value in table.get(key, ())
                if accept is None or accept(row_id, value)
            )
    return found, lookups


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
        candidate, probes = probe(rows, table, predicate, build_unique=build_unique)
        assert baseline == candidate
        observed_counts.append(probes)
    assert tuple(observed_counts) == expected_counts
    byte_values = [b"", b"A12", b"A13", b"A12\x00", b"x" * 12, b"x" * 13,
                   "短键".encode(), "短码".encode()]
    for a in byte_values:
        for b in byte_values:
            equal, route = short_equal(a, b)
            assert equal == (compare(a, b) == 0)
            assert route == ("inline" if max(len(a), len(b)) <= 12 else "fallback")

    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": True, "eligible_probes": observed_counts[0],
                               "byte_comparisons": len(byte_values)**2}) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
