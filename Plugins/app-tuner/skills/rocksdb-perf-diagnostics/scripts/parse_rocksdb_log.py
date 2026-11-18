#!/usr/bin/env python3
"""Summarize RocksDB LOG: write stalls, compactions, flushes.

Usage:
    python3 parse_rocksdb_log.py LOG [LOG.old.123 ...] [--json] [--samples N]

Only reads the log; does not touch the database.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

TS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2}\.\d+)")

PATTERNS = {
    "stall_slow_l0": re.compile(r"Stalling writes because we have (\d+) level-0 files"),
    "stall_slow_mem": re.compile(
        r"Stalling writes because we have (\d+) immutable memtables"
    ),
    "stall_slow_pending": re.compile(
        r"Stalling writes because of estimated pending compaction bytes (\d+)"
    ),
    "stop_l0": re.compile(r"Stopping writes because we have (\d+) level-0 files"),
    "stop_mem": re.compile(
        r"Stopping writes because we have (\d+) immutable memtables"
    ),
    "stop_pending": re.compile(
        r"Stopping writes because of estimated pending compaction bytes (\d+)"
    ),
    "compaction_start": re.compile(r"\[JOB (\d+)\] Compacting "),
    "compaction_done": re.compile(r"\[JOB (\d+)\] Compacted .* => (\d+) bytes"),
    "stats_stalls": re.compile(r"Stalls\(count\): (.+)$"),
    "background_error": re.compile(
        r"(?i)(background error|BackgroundError|corruption|IO error)"
    ),
}

FLUSH_START = re.compile(r"flush_started")
FLUSH_DONE = re.compile(r"flush_finished")


def parse_files(paths):
    counts = Counter()
    samples = defaultdict(list)
    l0_values = []
    compaction_bytes = 0
    compaction_jobs = set()
    flush_started = flush_finished = 0
    stats_stall_lines = []
    first_ts = last_ts = None

    for path in paths:
        try:
            fh = open(path, "r", errors="replace")
        except OSError as exc:
            print(f"warning: cannot open {path}: {exc}", file=sys.stderr)
            continue
        with fh:
            for line in fh:
                m = TS_RE.match(line)
                if m:
                    first_ts = first_ts or m.group(1)
                    last_ts = m.group(1)

                for name, pat in PATTERNS.items():
                    mm = pat.search(line)
                    if not mm:
                        continue
                    counts[name] += 1
                    if len(samples[name]) < 5:
                        samples[name].append(line.rstrip())

                    if name in ("stall_slow_l0", "stop_l0"):
                        l0_values.append(int(mm.group(1)))
                    if name == "compaction_done":
                        compaction_bytes += int(mm.group(2))
                        compaction_jobs.add(mm.group(1))
                    if name == "stats_stalls":
                        stats_stall_lines.append(line.rstrip())

                if FLUSH_START.search(line):
                    flush_started += 1
                if FLUSH_DONE.search(line):
                    flush_finished += 1

    l0_values.sort()
    return {
        "files": paths,
        "time_range": {"first": first_ts, "last": last_ts},
        "stalls": {
            name: counts[name]
            for name in (
                "stall_slow_l0",
                "stall_slow_mem",
                "stall_slow_pending",
                "stop_l0",
                "stop_mem",
                "stop_pending",
            )
        },
        "max_level0_files_seen": l0_values[-1] if l0_values else 0,
        "compaction": {
            "started": counts["compaction_start"],
            "finished": counts["compaction_done"],
            "output_bytes": compaction_bytes,
            "distinct_jobs": len(compaction_jobs),
        },
        "flush": {"started": flush_started, "finished": flush_finished},
        "background_error_lines": counts["background_error"],
        "samples": samples,
        "stats_stall_lines": stats_stall_lines[-3:],
    }


def print_human(r):
    tr = r["time_range"]
    print("== RocksDB LOG summary ==")
    print(f"files            : {', '.join(r['files'])}")
    print(f"time range       : {tr['first']} .. {tr['last']}")
    print(f"max L0 files seen: {r['max_level0_files_seen']}")
    print()
    print("-- write stalls --")
    for name, n in r["stalls"].items():
        if n:
            print(f"  {name:20s}: {n}")
    if not any(r["stalls"].values()):
        print("  none found")
    print()
    print("-- compaction --")
    c = r["compaction"]
    print(
        f"  started={c['started']} finished={c['finished']} "
        f"output={c['output_bytes'] / (1024 ** 3):.2f} GiB "
        f"distinct_jobs={c['distinct_jobs']}"
    )
    print()
    print("-- flush --")
    f = r["flush"]
    print(f"  started={f['started']} finished={f['finished']}")
    print()
    if r["background_error_lines"]:
        print(f"!! background/IO/corruption lines: {r['background_error_lines']}")
    if r["stats_stall_lines"]:
        print("-- last periodic Stalls(count) lines --")
        for line in r["stats_stall_lines"]:
            print(f"  {line}")
    print()
    print("-- sample lines --")
    for name, lines in r["samples"].items():
        if not lines:
            continue
        print(f"  [{name}]")
        for line in lines[:3]:
            print(f"    {line}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", help="LOG file(s)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument(
        "--samples", type=int, default=5, help="max sample lines per category"
    )
    args = ap.parse_args()

    result = parse_files(args.logs)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_human(result)


if __name__ == "__main__":
    main()
