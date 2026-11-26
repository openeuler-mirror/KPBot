"""Check packed boolean output compaction against row-wise filtering."""

from dataclasses import dataclass, replace
import json
from pathlib import Path


@dataclass(frozen=True)
class Context:
    inner: bool = True
    all_selected: bool = True
    identity: bool = True
    no_nulls: bool = True


def reference(flags, build, probe, active):
    return [(build[i], probe[i]) for i in active if flags[i] is True]


def set_bits(word):
    while word:
        lowest = word & -word
        yield lowest.bit_length() - 1
        word ^= lowest


def compact(flags, build, probe, active, context, dirty_tail=False):
    num_rows = len(flags)
    if not (context.inner and num_rows and context.all_selected and context.identity and context.no_nulls):
        return reference(flags, build, probe, active), "fallback"

    build_rows, probe_rows = list(build), list(probe)
    written = 0
    for start in range(0, num_rows, 64):
        end = min(num_rows, start + 64)
        word = sum(1 << (i - start) for i in range(start, end) if flags[i])
        if dirty_tail:
            word |= ((1 << 64) - 1) ^ ((1 << (end - start)) - 1)
        word &= (1 << (end - start)) - 1
        for offset in set_bits(word):
            source = start + offset
            build_rows[written], probe_rows[written] = build_rows[source], probe_rows[source]
            written += 1
    return list(zip(build_rows[:written], probe_rows[:written])), "bitmask"


def boundary_case(size, flags):
    # Pad both mappings, so a tail bug emits observable but invalid rows.
    build_rows = [1000 + i for i in range(size + 64)]
    probe_rows = [3 * i for i in range(size + 64)]
    active = list(range(size))
    actual, route = compact(flags, build_rows, probe_rows, active, Context(), dirty_tail=True)
    return route == "bitmask" and actual == reference(flags, build_rows, probe_rows, active)


def main():
    context = Context()
    flags = [False, True, False, True, True]
    build_rows, probe_rows = [800, 501, 200, 501, 900], [7, 9, 4, 9, 8]
    active = list(range(len(flags)))
    actual, route = compact(flags, build_rows, probe_rows, active, context)
    fallback_inputs = (
        (flags, active, replace(context, inner=False)),
        (flags, [1, 3], replace(context, all_selected=False)),
        (flags, active, replace(context, identity=False)),
        ([None, None, True, False, True], active, replace(context, no_nulls=False)),
    )
    checks = {
        "bitmask_route_positive": route == "bitmask" and actual == [(501, 9), (501, 9), (900, 8)],
        "dual_mapping_order_and_duplicates": actual == reference(flags, build_rows, probe_rows, active),
        "word_boundaries_and_dirty_tail": all(
            boundary_case(size, [i in (0, 3, 62, 64) for i in range(size)]) for size in (63, 64, 65)),
        "dense_and_empty_pass_masks": all(boundary_case(65, [value] * 65) for value in (True, False)),
        "unsafe_paths_fall_back": all(compact(f, build_rows, probe_rows, a, c)
                                       == (reference(f, build_rows, probe_rows, a), "fallback")
                                       for f, a, c in fallback_inputs),
        "empty_input": compact([], [], [], [], context) == ([], "fallback"),
    }
    success = all(checks.values())
    out = Path(__file__).resolve().parent / "results" / "result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": success, **checks}, indent=2) + "\n", encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
