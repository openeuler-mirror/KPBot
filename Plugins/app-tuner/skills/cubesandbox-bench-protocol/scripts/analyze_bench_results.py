#!/usr/bin/env python3
"""Analyze cube-bench JSON reports: per-round stats, shape classification,
per-scenario round aggregation, and epoch reminders.

Reads the report format written by cube-bench (v0.7.x): a JSON object with
``summary`` (total_time_s/successful/errors/success_rate/throughput_qps),
``create``/``delete`` stat blocks (min/max/avg/std/p50/p90/p95/p99) and a
``raw`` array of per-request ``{seq, create_ms[, error]}`` entries.

Rounds aggregate **per scenario** (concurrency/total/mode/template): a
results directory legitimately mixes c1 health anchors, throughput,
latency and template-A-vs-template-B runs, so pooling every file into
one median/CV would be meaningless - ``rounds.groups`` carries one
entry per scenario with its own count/median/CV/stability. Non-numeric
``success_rate`` values degrade to a per-file warning instead of
crashing the batch.

Shape classification over ``raw`` create_ms (heuristic thresholds, order
matters):

- ``quantized-tail``: a small late cluster packed in a narrow band well above
  the median -> a polling period / timeout is sleeping (e.g. 601ms tails
  from a 500ms poll + 100ms first probe);
- ``staircase``: max/min >= 2.0 with visible layering -> serialization
  queueing (lock / single-point service);
- ``uniform-lift``: max/min <= 1.5 -> shared-resource contention;
- ``mixed``: anything in between.

Usage:
    python3 analyze_bench_results.py --files run1.json run2.json
    python3 analyze_bench_results.py --dir results/
    python3 analyze_bench_results.py --dir results/ --output verdict.json

Exit 0 on a completed analysis with every report readable; 2 on bad
arguments (missing/invalid --root, no files); 3 when any report is
unreadable or not a bench schema - the remaining files are still analyzed
and the verdict still carries per-file errors plus a top-level "errors"
list, but a partial verdict must not be consumed as a pass. Python 3
standard library only.
"""

import argparse
import json
import math
import sys
from pathlib import Path

MIN_TAIL_SAMPLES = 3
TAIL_BAND_REL = 0.05     # tail band <= 5% of median
TAIL_BAND_ABS = 2.0      # ... or <= 2 ms
TAIL_LIFT = 1.5          # tail start >= 1.5 x median
TAIL_GAP_REL = 0.25      # gap between body and tail >= 25% of median
TAIL_GAP_ABS = 5.0       # ... or >= 5 ms
TAIL_MAX_RATIO = 0.15    # tail cluster <= 15% of samples
STAIRCASE_SPREAD = 2.0
UNIFORM_SPREAD = 1.5
MIN_ROUNDS = 3            # a group needs >= 3 rounds for a conclusive median
UNSTABLE_CV = 0.10


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _cv(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) / abs(mean)


def classify_shape(samples):
    """Return (shape, evidence) for a list of create_ms samples.

    A quantized-tail needs an isolated, narrow, small, high cluster: the
    separation from the body (largest adjacent gap above the median region)
    is what distinguishes it from a continuous high band (staircase).
    """
    if len(samples) < MIN_TAIL_SAMPLES:
        return "insufficient-data", {"count": len(samples)}
    ordered = sorted(samples)
    count = len(ordered)
    lo, hi = ordered[0], ordered[-1]
    median = _median(samples)
    spread = hi / lo if lo > 0 else float("inf")

    # largest adjacent gap searched above the median region so that noise
    # gaps inside the body never win
    search_from = max(MIN_TAIL_SAMPLES, count // 2)
    best_gap, best_idx = 0.0, count - 1
    for index in range(search_from, count - 1):
        gap = ordered[index + 1] - ordered[index]
        if gap > best_gap:
            best_gap, best_idx = gap, index + 1
    upper = ordered[best_idx:]
    upper_ratio = len(upper) / count

    evidence = {
        "spread_ratio": round(spread, 3),
        "median": round(median, 3),
        "tail_count": len(upper),
        "tail_lo": round(upper[0], 3) if upper else None,
        "tail_hi": round(upper[-1], 3) if upper else None,
        "tail_band": round(upper[-1] - upper[0], 3) if upper else None,
        "tail_gap": round(best_gap, 3),
        "tail_lift": round(upper[0] / median, 3) if upper and median else None,
        "high_cluster_ratio": round(upper_ratio, 4),
    }

    if (upper and best_gap >= max(TAIL_GAP_REL * median, TAIL_GAP_ABS)
            and upper_ratio <= TAIL_MAX_RATIO
            and upper[0] >= TAIL_LIFT * median
            and (upper[-1] - upper[0])
            <= max(TAIL_BAND_REL * median, TAIL_BAND_ABS)):
        return "quantized-tail", evidence
    if spread >= STAIRCASE_SPREAD:
        return "staircase", evidence
    if spread <= UNIFORM_SPREAD:
        return "uniform-lift", evidence
    return "mixed", evidence


def analyze_file(path):
    entry = {"file": str(path)}
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        entry["mtime"] = Path(path).stat().st_mtime
    except (OSError, ValueError) as exc:
        entry["error"] = "unreadable: %s" % exc
        return entry

    # schema guard: a cube-bench report must be an object whose blocks are
    # objects (raw: array). A stray non-bench json in --dir must degrade to
    # an entry-level error, never crash the whole verdict (review !114 P2).
    if not isinstance(report, dict):
        entry["error"] = ("unsupported schema: root is %s, expected object"
                          % type(report).__name__)
        return entry
    for block in ("summary", "create", "config"):
        value = report.get(block)
        if value is not None and not isinstance(value, dict):
            entry["error"] = ("unsupported schema: %s is %s, expected object"
                              % (block, type(value).__name__))
            return entry
    raw = report.get("raw")
    if raw is not None and not isinstance(raw, list):
        entry["error"] = ("unsupported schema: raw is %s, expected array"
                          % type(raw).__name__)
        return entry
    raw = raw or []

    warnings = []
    summary = report.get("summary") or {}
    create = report.get("create") or {}
    config = report.get("config") or {}
    entry["config"] = {key: config.get(key) for key in
                       ("concurrency", "total", "warmup", "mode", "template")}
    entry["summary"] = {key: summary.get(key) for key in
                        ("total_time_s", "successful", "errors",
                         "success_rate", "throughput_qps")}
    entry["create_stats"] = {key: create.get(key) for key in
                             ("count", "min", "avg", "p50", "p99", "max")}

    if summary.get("errors"):
        warnings.append("errors: %s" % summary.get("errors"))
    rate = summary.get("success_rate")
    if rate is not None and (isinstance(rate, bool)
                             or not isinstance(rate, (int, float))):
        warnings.append("success_rate non-numeric (%r) - ignored" % (rate,))
    elif isinstance(rate, (int, float)) and rate < 1.0:
        warnings.append("success_rate < 1.0 (%.3f)" % rate)

    samples = [item["create_ms"] for item in raw
               if isinstance(item, dict)
               and isinstance(item.get("create_ms"), (int, float))
               and not item.get("error")]
    if samples:
        shape, evidence = classify_shape(samples)
        entry["raw_count"] = len(samples)
        entry["shape"] = shape
        entry["shape_evidence"] = evidence
    else:
        entry["raw_count"] = 0
        warnings.append("no usable raw samples (old report format?)")

    entry["warnings"] = warnings
    return entry


def analyze_rounds(entries):
    """Aggregate rounds per scenario (concurrency, total, mode, template).

    A results directory legitimately mixes c1 health anchors, throughput
    and latency runs, and template-A-vs-template-B comparisons; pooling
    any of those together would be meaningless (and would fake a
    ">= 3 rounds" signal). Each scenario group keeps its own
    median/CV/stability; rounds semantics apply inside a group only.
    """
    usable = [entry for entry in entries
              if isinstance(entry.get("summary"), dict)
              and isinstance(entry["summary"].get("total_time_s"),
                             (int, float))
              and not isinstance(entry["summary"].get("total_time_s"), bool)]
    groups = {}
    order = []
    for entry in usable:
        config = entry.get("config") or {}
        key = (config.get("concurrency"), config.get("total"),
               config.get("mode"), config.get("template"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    group_reports = []
    for key in order:
        members = groups[key]
        totals = [member["summary"]["total_time_s"] for member in members]
        report = {
            "scenario": {"concurrency": key[0], "total": key[1],
                         "mode": key[2], "template": key[3]},
            "n": len(members),
            "rounds_sufficient": len(members) >= MIN_ROUNDS,
            "total_time_list": totals,
            "avg_list": [member.get("create_stats", {}).get("avg")
                         for member in members],
            "p50_list": [member.get("create_stats", {}).get("p50")
                         for member in members],
            "p99_list": [member.get("create_stats", {}).get("p99")
                         for member in members],
        }
        report["median_total_time"] = round(_median(totals), 4)
        if len(members) < MIN_ROUNDS:
            # a 1-2 round group must never surface as a verified "stable"
            # conclusion: the skill's own ">= 3 rounds" rule starts here
            report["median_total_time_note"] = ("fewer than %d rounds - "
                                                "indicative only"
                                                % MIN_ROUNDS)
            report["stability"] = ("insufficient rounds (n=%d) - do not "
                                   "conclude" % len(members))
        else:
            cv = _cv(totals)
            report["cv_total_time"] = round(cv, 4)
            report["stability"] = ("unstable (cv > %.0f%%) - stabilize the "
                                   "environment before concluding"
                                   % (UNSTABLE_CV * 100)
                                   if cv > UNSTABLE_CV else "stable")
        group_reports.append(report)

    return {
        "groups": group_reports,
        "total_files": len(usable),
        "note": "rounds aggregate per scenario (concurrency/total/mode/"
                "template); each group carries n/rounds_sufficient - a "
                "group with n < %d must only serve as a single-round "
                "reference, never as a verified conclusion; mixing c1 "
                "anchors / throughput / latency / template-A-vs-B runs in "
                "one directory is expected - each group keeps its own "
                "median/CV/stability" % MIN_ROUNDS,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Analyze cube-bench JSON reports.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--files", nargs="+",
                        help="report json files (in round order)")
    source.add_argument("--dir",
                        help="directory of report json files (mtime order)")
    parser.add_argument("--output",
                        help="write verdict json to this file as UTF-8")
    args = parser.parse_args(argv)

    paths = []
    if args.files:
        paths = [Path(name) for name in args.files]
    else:
        root = Path(args.dir)
        if not root.is_dir():
            print("error: --dir is not a directory: %s" % root,
                  file=sys.stderr)
            return 2
        paths = sorted((item for item in root.glob("*.json")
                        if item.is_file()), key=lambda p: p.stat().st_mtime)
        if not paths:
            print("error: no json files under %s" % root, file=sys.stderr)
            return 2
        if args.output:
            # never treat our own previous verdict as a bench report
            out_path = Path(args.output).resolve()
            paths = [item for item in paths
                     if item.resolve() != out_path]

    entries = [analyze_file(path) for path in paths]
    failures = [{"file": entry["file"], "error": entry["error"]}
                for entry in entries if entry.get("error")]
    verdict = {
        "files": entries,
        "rounds": analyze_rounds(entries),
        "notes": [
            "epoch discipline: only compare numbers from the same epoch "
            "(same component versions, kernel params, template, no reboot "
            "in between); a reboot alone can shift results by tens of "
            "percent",
            "interpretation: uniform-lift -> shared-resource contention; "
            "staircase -> serialization queueing; quantized-tail -> a "
            "polling period / timeout is sleeping",
            "shape thresholds are heuristics, not theorems",
        ],
    }
    if failures:
        verdict["errors"] = failures

    payload = json.dumps(verdict, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    else:
        print(payload)
    if failures:
        print("error: %d of %d report(s) unreadable or unsupported schema; "
              "verdict incomplete, exiting non-zero"
              % (len(failures), len(entries)), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
