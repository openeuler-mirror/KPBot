#!/usr/bin/env python3
"""Compare two top-k dumps for G3/G4 gates.

Input contract: text file, one row per (query, rank) result.
Accepted row forms (blank lines and lines starting with '#' are ignored):

    query_id,rank,doc_id,distance
    query_id rank doc_id distance
    query_id,doc_id,distance          # rank implied by file order

`doc_id` is compared as a string; `distance` is parsed as float.

Typical use:
    python3 compare_topk.py --baseline base.dump --candidate cand.dump --k 10

Exit code is 1 when any recall@k is below 1.0 or any matched distance is
outside tolerance (use --no-fail to only report).
"""

import argparse
import json
import math
import sys
from collections import OrderedDict

DEFAULTS = {
    "atol": 1e-6,
    "rtol": 1e-6,
    "max_report": 20,
}


def split_row(line):
    if "," in line:
        parts = [p.strip() for p in line.split(",")]
    else:
        parts = line.split()
    return [p for p in parts if p != ""]


def parse_dump(path, limit_k=None):
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
            if limit_k is not None and rank >= limit_k:
                continue
            data.setdefault(qid, []).append((rank, doc_id, dist))
    for qid in data:
        data[qid].sort(key=lambda x: x[0])
    return data


def rel_diff(a, b):
    if a == b:
        return 0.0
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom


def compare(base, cand, k, atol, rtol, max_report):
    qids = list(base.keys())
    report = []
    total_recall = 0.0
    min_recall = 1.0
    exact_order = 0
    dist_mismatch = 0
    max_abs = 0.0
    max_rel = 0.0
    id_mismatch_queries = 0

    for qid in qids:
        b_rows = base[qid][:k]
        c_rows = cand.get(qid, [])[:k]
        b_ids = [r[1] for r in b_rows]
        c_ids = [r[1] for r in c_rows]
        b_set = set(b_ids)
        c_set = set(c_ids)
        recall = 0.0 if not b_set else len(b_set & c_set) / float(len(b_set))
        total_recall += recall
        min_recall = min(min_recall, recall)
        same_order = b_ids == c_ids
        if same_order:
            exact_order += 1
        elif b_set != c_set:
            id_mismatch_queries += 1

        query_dist_bad = False
        for pos, (b_row, c_row) in enumerate(zip(b_rows, c_rows)):
            if b_row[1] != c_row[1]:
                continue
            bd, cd = b_row[2], c_row[2]
            ad = abs(bd - cd)
            rd = rel_diff(bd, cd)
            max_abs = max(max_abs, ad)
            max_rel = max(max_rel, rd)
            if not (math.isclose(bd, cd, rel_tol=rtol, abs_tol=atol)):
                dist_mismatch += 1
                query_dist_bad = True

        if (not same_order) or recall < 1.0 or query_dist_bad:
            if len(report) < max_report:
                report.append(
                    {
                        "query_id": qid,
                        "recall": recall,
                        "exact_order": same_order,
                        "baseline": [{"rank": r, "id": i, "dist": d} for r, i, d in b_rows],
                        "candidate": [{"rank": r, "id": i, "dist": d} for r, i, d in c_rows],
                    }
                )

    n = len(qids)
    summary = {
        "queries": n,
        "k": k,
        "mean_recall": (total_recall / n) if n else 0.0,
        "min_recall": min_recall if n else 0.0,
        "exact_order_queries": exact_order,
        "id_mismatch_queries": id_mismatch_queries,
        "distance_mismatches": dist_mismatch,
        "max_abs_distance_diff": max_abs,
        "max_rel_distance_diff": max_rel,
        "missing_candidate_queries": sum(1 for q in qids if q not in cand),
        "atol": atol,
        "rtol": rtol,
    }
    return summary, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="baseline top-k dump")
    ap.add_argument("--candidate", required=True, help="candidate/migrated top-k dump")
    ap.add_argument("--k", type=int, default=None, help="compare only top-k (default: full dump)")
    ap.add_argument("--atol", type=float, default=DEFAULTS["atol"])
    ap.add_argument("--rtol", type=float, default=DEFAULTS["rtol"])
    ap.add_argument("--max-report", type=int, default=DEFAULTS["max_report"])
    ap.add_argument("--json", help="write machine-readable report to this path")
    ap.add_argument("--no-fail", action="store_true", help="always exit 0 unless parse error")
    args = ap.parse_args(argv)

    try:
        base = parse_dump(args.baseline, args.k)
        cand = parse_dump(args.candidate, args.k)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if not base:
        print("error: baseline dump is empty", file=sys.stderr)
        return 2

    k = args.k
    if k is None:
        k = max(len(rows) for rows in base.values())

    summary, report = compare(base, cand, k, args.atol, args.rtol, args.max_report)
    result = {"summary": summary, "mismatches": report}

    print(
        "queries=%d k=%d mean_recall=%.6f min_recall=%.6f exact_order=%d/%d "
        "id_mismatch_queries=%d dist_mismatch=%d max_abs=%.6g max_rel=%.6g missing_candidate=%d"
        % (
            summary["queries"],
            summary["k"],
            summary["mean_recall"],
            summary["min_recall"],
            summary["exact_order_queries"],
            summary["queries"],
            summary["id_mismatch_queries"],
            summary["distance_mismatches"],
            summary["max_abs_distance_diff"],
            summary["max_rel_distance_diff"],
            summary["missing_candidate_queries"],
        )
    )

    for item in report:
        print(
            "  qid=%s recall=%.3f exact_order=%s"
            % (item["query_id"], item["recall"], item["exact_order"])
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("report_json=%s" % args.json)

    failed = (
        summary["min_recall"] < 1.0
        or summary["distance_mismatches"] > 0
        or summary["missing_candidate_queries"] > 0
    )
    if failed and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
