"""Check exact integer-widening filters and preserved-side fallback."""

from collections import Counter
import json
from pathlib import Path

LOWER, UPPER = -(2**31), 2**31 - 1


def join(sales, dates, preserve_probe=False):
    result = []
    for sale, key in sales:
        matches = () if key is None else dates.get(key, ())
        if matches:
            result.extend((sale, date) for date in matches)
        elif preserve_probe:
            result.append((sale, None))
    return result


def candidates(sales, dates, preserve_probe=False):
    if preserve_probe:
        return sales
    source_values = {key for key in dates if key is not None and LOWER <= key <= UPPER}
    return [(sale, key) for sale, key in sales if key in source_values]


def main():
    sales = [(1, 99), (2, 101), (3, 101), (4, 102), (5, None), (6, LOWER), (7, UPPER)]
    date_sets = (
        {101: ("May 1", "May 1"), 102: ("May 2",), UPPER + 1: ("outside",),
         None: ("null build key",)},
        {LOWER: ("lower",), UPPER: ("upper",), LOWER - 1: ("outside",)},
        {},
    )
    counts = []
    false_negatives = 0
    for dates in date_sets:
        filtered = candidates(sales, dates)
        original_matches = Counter(join(sales, dates))
        filtered_matches = Counter(join(filtered, dates))
        false_negatives += sum((original_matches - filtered_matches).values())
        assert original_matches == filtered_matches
        counts.append(len(filtered))
    assert counts == [3, 2, 0]
    outer_input = candidates(sales, date_sets[0], preserve_probe=True)
    assert outer_input == sales
    outer_result = Counter(join(outer_input, date_sets[0], preserve_probe=True))
    assert outer_result[(1, None)] == 1 and outer_result[(5, None)] == 1
    assert outer_result[(2, "May 1")] == 2
    result = {
        "success": True, "false_negatives": false_negatives,
        "scan_outputs": counts, "outer_preserved_rows": len(outer_input),
    }
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
