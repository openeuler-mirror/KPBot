#!/usr/bin/env python3
"""Compare two top-k dumps for the G0/G3 equivalence gates.

Input contract: text file, one row per (query, rank) result.
Accepted row forms (blank lines and lines starting with '#' are ignored):

    query_id,rank,doc_id,distance
    query_id rank doc_id distance
    query_id,doc_id,distance          # rank implied by file order

`doc_id` is compared as a string; `distance` is parsed as float.

This tool checks baseline-vs-candidate equivalence, NOT recall against
ground truth: the reported overlap@k is the set overlap of the two
top-k lists. For G4, compute recall@k of each arm against the frozen
ground truth separately; do not use baseline overlap as recall.

Default (strict, for G0/G3 "bit-exact" equivalence): the two dumps must
agree on the query set (both directions), per-query row counts, rank
columns (contiguous, no duplicates on either side), doc-id order, and
distances compared by doc id. Distance equality in strict mode (the
default --atol 0 --rtol 0) is BIT-PATTERN equality, not numeric
equality: baseline -0.0 vs candidate +0.0 (or a NaN payload change)
is a mismatch. Truncation to top-k happens ONLY when --k is passed
explicitly; by default every row of every query is compared, so a
candidate returning extra rows fails instead of being silently cut.

Relaxations are explicit and must be justified by the mechanism under
test:

    --allow-order-change   accept order/tie-break changes (OPT_IN
                           mechanisms); still requires identical doc-id
                           sets and distances
    --atol/--rtol          distance tolerance (default 0 = bit-exact;
                           with a nonzero tolerance the comparison is
                           numeric, so -0.0 vs +0.0 passes)

Typical use:
    python3 compare_topk.py --baseline base.dump --candidate cand.dump --k 10

Exit codes: 0 = equivalent under the requested strictness; 1 = any
mismatch (query set, row count, rank, order, id set, distance); 2 =
parse error. --no-fail keeps exit 0 for report-only runs.
"""

import argparse
import json
import math
import struct
import sys
from collections import OrderedDict

DEFAULTS = {
    "atol": 0.0,
    "rtol": 0.0,
    "max_report": 20,
}


def split_row(line):
    if "," in line:
        parts = [p.strip() for p in line.split(",")]
    else:
        parts = line.split()
    return [p for p in parts if p != ""]


def parse_dump(path):
    data = OrderedDict()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = split_row(line)
            if len(parts) == 4:
                qid, rank, doc_id, dist = parts
                rank = int(rank)
            elif len(parts) == 3:
                qid, doc_id, dist = parts
                rank = len(data.get(qid, []))
            else:
                raise ValueError(
                    "%s:%d: expected 3 or 4 fields, got %d: %r"
                    % (path, lineno, len(parts), line)
                )
            try:
                dist = float(dist)
            except ValueError:
                raise ValueError("%s:%d: bad distance %r" % (path, lineno, dist))
            data.setdefault(qid, []).append((rank, doc_id, dist))
    for qid in data:
        data[qid].sort(key=lambda x: x[0])
    return data


def rel_diff(a, b):
    if a == b:
        return 0.0
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def dist_bits(x):
    return struct.pack(">d", x)


def dist_equal(bd, cd, atol, rtol):
    """Strict mode (atol == rtol == 0) compares IEEE-754 bit patterns, so
    -0.0 vs +0.0 and NaN payload changes are mismatches; a nonzero
    tolerance falls back to numeric comparison."""
    if atol == 0.0 and rtol == 0.0:
        return dist_bits(bd) == dist_bits(cd)
    return math.isclose(bd, cd, rel_tol=rtol, abs_tol=atol)


def ranks_valid(rows):
    """A rank column must be contiguous and duplicate-free (0- or 1-based
    both accepted); the 3-field row form assigns implicit ranks."""
    ranks = [r for r, _, _ in rows]
    if not ranks:
        return True
    lo = min(ranks)
    return sorted(ranks) == list(range(lo, lo + len(ranks)))


def compare(base, cand, k, atol, rtol, max_report, allow_order_change):
    base_qids = list(base.keys())
    cand_qids = list(cand.keys())
    base_qset = set(base_qids)
    cand_qset = set(cand_qids)

    report = []
    total_overlap = 0.0
    min_overlap = 1.0
    exact_order = 0
    order_mismatch = 0
    row_count_mismatch = 0
    rank_mismatch = 0
    dist_mismatch = 0
    id_mismatch_queries = 0
    max_abs = 0.0
    max_rel = 0.0

    for qid in base_qids:
        b_rows = base[qid][:k]
        c_rows = cand.get(qid, [])[:k]
        b_ids = [r[1] for r in b_rows]
        c_ids = [r[1] for r in c_rows]
        b_set = set(b_ids)
        c_set = set(c_ids)
        overlap = 0.0 if not b_set else len(b_set & c_set) / float(len(b_set))
        total_overlap += overlap
        min_overlap = min(min_overlap, overlap)

        same_order = b_ids == c_ids
        if same_order:
            exact_order += 1
        else:
            order_mismatch += 1
        if b_set != c_set:
            id_mismatch_queries += 1
        if len(b_rows) != len(c_rows):
            row_count_mismatch += 1
        rank_ok = ranks_valid(b_rows) and ranks_valid(c_rows)
        if not rank_ok:
            rank_mismatch += 1

        # Distance comparison aligned by doc_id (independent of position):
        # every id present on both sides must agree within tolerance; ids
        # present on only one side are counted as id set mismatches above.
        b_map = {}
        for _, doc_id, dist in b_rows:
            b_map.setdefault(doc_id, []).append(dist)
        c_map = {}
        for _, doc_id, dist in c_rows:
            c_map.setdefault(doc_id, []).append(dist)
        query_dist_bad = False
        for doc_id, b_dists in b_map.items():
            c_dists = c_map.get(doc_id)
            if c_dists is None or len(c_dists) != len(b_dists):
                continue
            for bd, cd in zip(b_dists, c_dists):
                ad = abs(bd - cd)
                rd = rel_diff(bd, cd)
                max_abs = max(max_abs, ad)
                max_rel = max(max_rel, rd)
                if not dist_equal(bd, cd, atol, rtol):
                    dist_mismatch += 1
                    query_dist_bad = True

        if (
            (not same_order and not allow_order_change)
            or b_set != c_set
            or len(b_rows) != len(c_rows)
            or not rank_ok
            or query_dist_bad
        ):
            if len(report) < max_report:
                report.append(
                    {
                        "query_id": qid,
                        "overlap": overlap,
                        "exact_order": same_order,
                        "rank_valid": rank_ok,
                        "baseline_rows": len(b_rows),
                        "candidate_rows": len(c_rows),
                        "baseline": [{"rank": r, "id": i, "dist": d} for r, i, d in b_rows],
                        "candidate": [{"rank": r, "id": i, "dist": d} for r, i, d in c_rows],
                    }
                )

    missing_candidate = [q for q in base_qids if q not in cand_qset]
    extra_candidate = [q for q in cand_qids if q not in base_qset]
    n = len(base_qids)
    summary = {
        "queries": n,
        "k": k,
        "mean_overlap": (total_overlap / n) if n else 0.0,
        "min_overlap": min_overlap if n else 0.0,
        "overlap_metric": "overlap@k (set overlap vs baseline, not recall vs ground truth)",
        "exact_order_queries": exact_order,
        "order_mismatch_queries": order_mismatch,
        "row_count_mismatch_queries": row_count_mismatch,
        "rank_mismatch_queries": rank_mismatch,
        "id_mismatch_queries": id_mismatch_queries,
        "distance_mismatches": dist_mismatch,
        "max_abs_distance_diff": max_abs,
        "max_rel_distance_diff": max_rel,
        "missing_candidate_queries": len(missing_candidate),
        "extra_candidate_queries": len(extra_candidate),
        "allow_order_change": bool(allow_order_change),
        "atol": atol,
        "rtol": rtol,
    }
    return summary, report, {"missing_candidate": missing_candidate, "extra_candidate": extra_candidate}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="baseline top-k dump")
    ap.add_argument("--candidate", required=True, help="candidate/migrated top-k dump")
    ap.add_argument("--k", type=int, default=None,
                    help="truncate both sides to the top-k rows per query "
                         "(default: full-dump comparison, no truncation)")
    ap.add_argument("--atol", type=float, default=DEFAULTS["atol"],
                    help="absolute distance tolerance (default 0 = bit-exact comparison)")
    ap.add_argument("--rtol", type=float, default=DEFAULTS["rtol"],
                    help="relative distance tolerance (default 0 = bit-exact comparison)")
    ap.add_argument("--allow-order-change", action="store_true",
                    help="accept order/tie-break changes (OPT_IN mechanisms); "
                         "doc-id sets and distances must still match")
    ap.add_argument("--max-report", type=int, default=DEFAULTS["max_report"])
    ap.add_argument("--json", help="write machine-readable report to this path")
    ap.add_argument("--no-fail", action="store_true", help="always exit 0 unless parse error")
    args = ap.parse_args(argv)

    try:
        # Truncation to top-k happens row-count-wise in compare(), not by
        # rank value, so both 0-based and 1-based rank columns behave the
        # same way.
        base = parse_dump(args.baseline)
        cand = parse_dump(args.candidate)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if not base:
        print("error: baseline dump is empty", file=sys.stderr)
        return 2

    # No implicit truncation: omitting --k compares every row of every
    # query on both sides, so a candidate returning extra rows fails
    # instead of being silently cut to the baseline's row count.
    k = args.k

    summary, report, qid_lists = compare(
        base, cand, k, args.atol, args.rtol, args.max_report, args.allow_order_change
    )
    result = {"summary": summary, "mismatches": report}

    print(
        "queries=%d k=%s mean_overlap=%.6f min_overlap=%.6f exact_order=%d/%d "
        "order_mismatch=%d row_count_mismatch=%d rank_mismatch=%d id_mismatch_queries=%d dist_mismatch=%d "
        "max_abs=%.6g max_rel=%.6g missing_candidate=%d extra_candidate=%d%s"
        % (
            summary["queries"],
            summary["k"] if summary["k"] is not None else "full",
            summary["mean_overlap"],
            summary["min_overlap"],
            summary["exact_order_queries"],
            summary["queries"],
            summary["order_mismatch_queries"],
            summary["row_count_mismatch_queries"],
            summary["rank_mismatch_queries"],
            summary["id_mismatch_queries"],
            summary["distance_mismatches"],
            summary["max_abs_distance_diff"],
            summary["max_rel_distance_diff"],
            summary["missing_candidate_queries"],
            summary["extra_candidate_queries"],
            " [order-change allowed]" if args.allow_order_change else "",
        )
    )

    for item in report:
        print(
            "  qid=%s overlap=%.3f exact_order=%s rank_valid=%s rows=%d/%d"
            % (item["query_id"], item["overlap"], item["exact_order"], item["rank_valid"],
               item["baseline_rows"], item["candidate_rows"])
        )
    for qid in qid_lists["missing_candidate"][: args.max_report]:
        print("  MISSING_IN_CANDIDATE qid=%s" % qid)
    for qid in qid_lists["extra_candidate"][: args.max_report]:
        print("  EXTRA_IN_CANDIDATE qid=%s" % qid)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("report_json=%s" % args.json)

    failed = (
        (summary["order_mismatch_queries"] > 0 and not args.allow_order_change)
        or summary["row_count_mismatch_queries"] > 0
        or summary["rank_mismatch_queries"] > 0
        or summary["id_mismatch_queries"] > 0
        or summary["distance_mismatches"] > 0
        or summary["missing_candidate_queries"] > 0
        or summary["extra_candidate_queries"] > 0
    )
    if failed:
        print(
            "exit=1: query set / row count / rank / order / id set / distance mismatch "
            "(strict=%s, atol=%g rtol=%g, order-change %s)"
            % (
                "bit-pattern" if (args.atol == 0 and args.rtol == 0) else "numeric",
                args.atol,
                args.rtol,
                "allowed" if args.allow_order_change else "rejected",
            )
        )
    if failed and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
