"""Compare post-filter pair identities; pre-filter candidates measure work only."""

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3


INT_MIN = -(1 << 31)
INT_MAX = (1 << 31) - 1


@dataclass(frozen=True)
class Context:
    inner: bool = True
    canonical_int_lt: bool = True
    direct_bindings: bool = True
    flat: bool = True
    complete_build: bool = True


def select_probes(build, probe, active, context):
    fallback = (active, "fallback")
    if not all((context.inner, context.canonical_int_lt, context.direct_bindings,
                context.flat, context.complete_build)):
        return fallback
    if max(Counter(key for key, _ in build).values(), default=0) < 2:
        return fallback
    if any(qty is None for _, qty in probe) or max(active, default=-1) + 1 < 32:
        return fallback

    bound = max((qty for _, qty in build if qty is not None), default=INT_MIN)
    if sum(qty >= bound for _, qty in probe[:32]) < 8:
        return fallback
    return [i for i in active if probe[i][1] < bound], "quantity-bound"


def original_pairs(build, probe, active):
    """Pairs surviving ordinary key equality and the remaining strict predicate."""
    return [(i, j) for i in active for j, (key, qty) in enumerate(build)
            if probe[i][0] is not None and key is not None and probe[i][0] == key and probe[i][1] is not None
            and qty is not None and probe[i][1] < qty]


def candidate_pairs(build, probe, active):
    """Before the remaining quantity predicate; pruning can reduce these candidates."""
    return [(i, j) for i in active for j, (key, _) in enumerate(build)
            if probe[i][0] is not None and key is not None and probe[i][0] == key]


def sqlite_pairs(build, probe, active):
    """Independent SQL oracle for ordinary = join and strict quantity <."""
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE b(id INTEGER, item TEXT, qty INTEGER)")
        db.execute("CREATE TABLE p(id INTEGER, item TEXT, qty INTEGER, active INTEGER)")
        db.executemany("INSERT INTO b VALUES(?,?,?)", [(i, key, qty) for i, (key, qty) in enumerate(build)])
        db.executemany("INSERT INTO p VALUES(?,?,?,?)", [(i, key, qty, int(i in active))
                       for i, (key, qty) in enumerate(probe)])
        return db.execute("""SELECT p.id,b.id FROM p JOIN b ON p.item=b.item
                             WHERE p.active=1 AND p.qty<b.qty ORDER BY p.id,b.id""").fetchall()


def main():
    context = Context()
    build = [("A", 10), ("A", 10), ("B", 3), ("B", None)]
    probe = [("A", 10), ("A", 9), ("B", 9), ("A", 11), ("C", 0)] * 13
    active = [i for i in range(len(probe)) if i not in {1, 19}]
    selected, route = select_probes(build, probe, active, context)
    expected = original_pairs(build, probe, active)
    changes = (replace(context, inner=False), replace(context, flat=False),
               replace(context, direct_bindings=False), replace(context, canonical_int_lt=False),
               replace(context, complete_build=False))

    # A complete build can contain a later match above the observed partial bound.
    complete = build + [("A", 20)]
    spill_selected, spill_route = select_probes(build, probe, active, changes[-1])
    all_null = [("A", None), ("A", None)]
    signed_probe = [("A", INT_MAX), ("A", INT_MIN)] * 16
    signed_build = [("A", INT_MAX), ("A", INT_MIN)]
    signed_active = list(range(32))
    signed_selected, signed_route = select_probes(signed_build, signed_probe, signed_active, context)
    null_selected, null_route = select_probes(all_null, signed_probe, signed_active, context)
    null_key_build = [(None, 10), (None, 10), ("A", 10), ("A", 10)]
    null_key_probe = [(None, 9), ("A", 9), ("A", 10)] * 11
    null_key_active = list(range(len(null_key_probe)))
    null_key_selected, null_key_route = select_probes(null_key_build, null_key_probe, null_key_active, context)
    expected_null_key_pairs = [(i, j) for i in range(1, 33, 3) for j in (2, 3)]
    changed_probe = [("A", 20), ("A", 10), ("A", 1)] * 11
    changed_active = list(range(len(changed_probe)))
    high_build, low_build = [("A", 20)] * 2, [("A", 5)] * 2
    high_selected, high_route = select_probes(high_build, changed_probe, changed_active, context)
    low_selected, low_route = select_probes(low_build, changed_probe, changed_active, context)
    baseline_candidates = candidate_pairs(build, probe, active)
    pruned_candidates = candidate_pairs(build, probe, selected)
    equal_build, equal_probe = [("A", 10)], [("A", 10)]
    # Single-pair semantic example; actual economic gates are checked separately.
    equal_selected = [i for i in [0] if equal_probe[i][1] < 10]
    gated_build, gated_probe = [("A", 10)] * 2, [("A", 10)] * 32
    gated_selected, gated_route = select_probes(gated_build, gated_probe, list(range(32)), context)

    checks = {
        "quantity_bound_route_positive": route == "quantity-bound" and len(selected) < len(active),
        "pair_order_and_multiplicity": original_pairs(build, probe, selected) == expected
        and expected[:2] == [(6, 0), (6, 1)],
        "strict_boundary_and_active_mask": 0 not in selected and 3 not in selected and 1 not in selected
        and 64 in selected and all(i in active for i in selected),
        "cross_key_still_filtered": 2 in selected and all(i != 2 for i, _ in expected),
        "unsupported_inputs_fall_back": all(select_probes(build, probe, active, item) == (active, "fallback")
                                            for item in changes),
        "spill_preserves_later_match": spill_route == "fallback"
        and (0, 4) in original_pairs(complete, probe, spill_selected),
        "null_and_empty_build": null_route == "quantity-bound" and not null_selected
        and not original_pairs(all_null, signed_probe, signed_active)
        and select_probes([], probe, active, context) == (active, "fallback"),
        "signed_extremes": signed_route == "quantity-bound" and signed_selected == list(range(1, 32, 2))
        and original_pairs(signed_build, signed_probe, signed_selected)
        == original_pairs(signed_build, signed_probe, signed_active),
        "pre_filter_candidates_are_workload": len(pruned_candidates) < len(baseline_candidates)
        and original_pairs(build, probe, selected) == expected,
        "equal_quantity_changes_work_not_result": candidate_pairs(equal_build, equal_probe, [0]) == [(0, 0)]
        and candidate_pairs(equal_build, equal_probe, equal_selected) == []
        and original_pairs(equal_build, equal_probe, [0]) == original_pairs(equal_build, equal_probe, equal_selected) == []
        and gated_route == "quantity-bound" and gated_selected == []
        and len(candidate_pairs(gated_build, gated_probe, list(range(32)))) == 64
        and original_pairs(gated_build, gated_probe, list(range(32))) == [],
        "economic_gates_and_nullable_fallback": all(
            select_probes(b, p, list(range(len(p))), context)[1] == "fallback"
            for b, p in ((build, [("A", 1)] * 32), (build, probe[:16]),
                         (build, [("A", None)] + probe[1:]), ([("A", 10)], probe))),
        "null_join_keys_do_not_match": null_key_route == "quantity-bound"
        and original_pairs(null_key_build, null_key_probe, null_key_active) == expected_null_key_pairs
        and original_pairs(null_key_build, null_key_probe, null_key_selected) == expected_null_key_pairs,
        "hash_table_change_recomputes_bound": high_route == low_route == "quantity-bound"
        and high_selected != low_selected and low_selected == list(range(2, 33, 3))
        and original_pairs(low_build, changed_probe, low_selected)
        == original_pairs(low_build, changed_probe, changed_active),
        "sqlite_reference_agrees": sqlite_pairs(build, probe, active) == expected
        and sqlite_pairs(null_key_build, null_key_probe, null_key_active) == expected_null_key_pairs
        and sqlite_pairs(signed_build, signed_probe, signed_active)
        == original_pairs(signed_build, signed_probe, signed_active),
    }
    success = all(checks.values())
    out = Path(__file__).resolve().parent / "results" / "result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"success": success, **checks,
        "equal_boundary_baseline_candidates": len(candidate_pairs(equal_build, equal_probe, [0])),
        "equal_boundary_pruned_candidates": len(candidate_pairs(equal_build, equal_probe, equal_selected)),
        "equal_boundary_post_filter_pairs": len(original_pairs(equal_build, equal_probe, equal_selected)),
        "baseline_candidate_pairs": len(baseline_candidates), "pruned_candidate_pairs": len(pruned_candidates)
    }, indent=2) + "\n", encoding="utf-8")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
