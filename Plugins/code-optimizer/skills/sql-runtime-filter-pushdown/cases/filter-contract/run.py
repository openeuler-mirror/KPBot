"""Check exact integer-widening filters and preserved-side fallback."""

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

LOWER, UPPER = -(2**31), 2**31 - 1


@dataclass(frozen=True)
class Projection:
    source_channel: int
    source_bits: int
    output_bits: int


@dataclass(frozen=True)
class Bloom:
    """Small deterministic membership model, not an engine Bloom implementation."""

    width_bytes: int
    slots: frozenset

    @staticmethod
    def slot(value, width_bytes):
        encoded = value.to_bytes(width_bytes, "little", signed=True)
        digest = hashlib.blake2b(encoded, digest_size=8).digest()
        return int.from_bytes(digest, "little") % 257

    @classmethod
    def build(cls, values, width_bytes):
        return cls(width_bytes, frozenset(cls.slot(value, width_bytes) for value in values))

    def allows(self, value):
        return value is not None and self.slot(value, self.width_bytes) in self.slots


def resolve_filter(projections, probe_channel, kind, payload, *, join_kind="inner", projection_errors_safe=True):
    """Use one mapping for capability checks and installation decisions."""
    if join_kind != "inner":
        return None, None, "preserved-probe-side"
    if not projection_errors_safe:
        return None, None, "projection-error-semantics"
    projection = projections[probe_channel]
    if projection.source_bits != projection.output_bits and (
        projection.source_bits, projection.output_bits
    ) != (32, 64):
        return None, None, "unsupported-cast"
    if kind == "values":
        lower = -(2 ** (projection.source_bits - 1))
        upper = 2 ** (projection.source_bits - 1) - 1
        values = {v for v in payload if v is not None and lower <= v <= upper}
        return projection.source_channel, lambda value: value in values, "installed-values"
    if kind == "bloom":
        if payload.width_bytes != projection.output_bits // 8:
            return None, None, "invalid-bloom-key-type"
        if payload.width_bytes != projection.source_bits // 8:
            return None, None, "incompatible-bloom-representation"
        return projection.source_channel, payload.allows, "installed-bloom"
    return None, None, "unsupported-filter"


def filter_scan(rows, projections, probe_channel, kind, payload, **safety):
    channel, predicate, reason = resolve_filter(projections, probe_channel, kind, payload, **safety)
    if predicate is None:
        return list(rows), channel, 0, reason
    return [row for row in rows if predicate(row[channel])], channel, len(rows), reason


def projection_and_bloom_checks():
    # Scan columns are (date_id, decoy, sale_id); the projection swaps outputs:
    # output 0 = widened decoy, output 1 = widened date_id (the join probe key).
    rows = [(99, 101, 1), (101, 99, 2), (101, 99, 3), (102, 99, 4), (None, 101, 5)]
    projection = [Projection(1, 32, 64), Projection(0, 32, 64)]
    dates = {101: ("May 1", "May 1"), 102: ("May 2",)}

    def matches(scan_rows):
        return Counter(join([(sale_id, date_id) for date_id, _, sale_id in scan_rows], dates))

    baseline = matches(rows)
    filtered, channel, applications, reason = filter_scan(rows, projection, 1, "values", dates)
    assert reason == "installed-values" and channel == 0 and applications == len(rows)
    assert [row[2] for row in filtered] == [2, 3, 4]
    assert matches(filtered) == baseline
    # A wrong-channel implementation must be detectable by this fixture.
    wrong_column = [row for row in rows if row[1] in dates]
    assert matches(wrong_column) != baseline
    assert resolve_filter(projection, 1, "values", dates)[0] == channel

    wide_bloom = Bloom.build(dates, 8)
    narrow_bloom = Bloom.build(dates, 4)
    assert all(wide_bloom.allows(key) for key in dates)
    assert any(narrow_bloom.slot(key, 4) not in wide_bloom.slots for key in dates)
    # Incompatible source encoding must leave the scan unchanged and install
    # no predicate; the ordinary join still verifies membership and duplicates.
    fallback, fallback_channel, incompatible_apps, fallback_reason = filter_scan(
        rows, projection, 1, "bloom", wide_bloom
    )
    assert fallback_reason == "incompatible-bloom-representation"
    assert fallback_channel is None and incompatible_apps == 0
    assert fallback == rows and matches(fallback) == baseline

    same_type_projection = [Projection(1, 32, 32), Projection(0, 32, 32)]
    compatible, compatible_channel, compatible_apps, compatible_reason = filter_scan(
        rows, same_type_projection, 1, "bloom", narrow_bloom
    )
    assert compatible_reason == "installed-bloom" and compatible_channel == 0
    assert compatible_apps == len(rows) and matches(compatible) == baseline
    assert len(compatible) < len(rows)
    return {
        "projection_source_channel": channel,
        "projection_scan_outputs": len(filtered),
        "projection_filter_applications": applications,
        "bloom_incompatible_fallbacks": 1,
        "bloom_incompatible_filter_applications": incompatible_apps,
        "bloom_fallback_preserved_rows": len(fallback),
        "bloom_compatible_filter_applications": compatible_apps,
    }


def anti_and_projection_error_checks():
    projection = [Projection(0, 32, 64)]
    # Ordinary NOT EXISTS must keep a probe row whose key is absent in build.
    build = {1: ("match",)}
    rows = [(2,)]
    scanned, channel, anti_apps, reason = filter_scan(
        rows, projection, 0, "values", build, join_kind="anti"
    )
    assert scanned == rows and channel is None and anti_apps == 0
    assert reason == "preserved-probe-side"
    anti_output = [row for row in scanned if row[0] is None or not build.get(row[0])]
    assert anti_output == [(2,)]
    assert [row for row in rows if row[0] in build] == []  # Unsafe positive filter.

    # This engine model eagerly evaluates the whole projection before joining.
    # The key widening is safe, but a different expression can throw on a row
    # that the join would otherwise reject.
    error_rows = [(2, "invalid"), (1, "7")]
    preserved, _, error_apps, error_reason = filter_scan(
        error_rows, projection, 0, "values", build, projection_errors_safe=False
    )
    assert preserved == error_rows and error_apps == 0
    assert error_reason == "projection-error-semantics"

    def evaluate(scan_rows):
        try:
            projected = [(key, int(text)) for key, text in scan_rows]
        except ValueError as error:
            return "error", type(error).__name__, str(error)
        return "ok", [row for row in projected if row[0] in build]

    original_error = evaluate(error_rows)
    assert original_error[0] == "error" and evaluate(preserved) == original_error
    # Prove the fixture detects the tempting key-only rewrite hiding the error.
    unsafe_rows, _, unsafe_apps, _ = filter_scan(error_rows, projection, 0, "values", build)
    assert unsafe_apps == 2 and evaluate(unsafe_rows) == ("ok", [(1, 7)])
    return {
        "anti_unmatched_output_rows": len(anti_output),
        "anti_filter_applications": anti_apps,
        "projection_error_preserved": evaluate(preserved) == original_error,
        "projection_error_filter_applications": error_apps,
    }


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
    result.update(projection_and_bloom_checks())
    result.update(anti_and_projection_error_checks())
    out = Path(__file__).resolve().parent / "results/result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
