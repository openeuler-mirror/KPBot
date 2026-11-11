#!/usr/bin/env python3
"""Validate completeness of trace_view.json and emit completeness_report.json."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_RULES = {
    "expect_processes": [
        "Python",
        "CANN",
        "Ascend Hardware",
        "Overlap Analysis",
        "Communication",
    ],
    "expect_overlap_tracks": ["Computing", "Communication", "Free"],
    "expect_categories": ["python_function", "cpu_op", "enqueue", "dequeue"],
    "min_events": 1000,
    "window_ms": {"target": 3000, "tolerance": 1500},
    "require_node_launch": True,
    "require_flow_balance": True,
    "warn_missing_counter": True,
}


def load_rules(path: str | None) -> dict[str, Any]:
    rules = dict(DEFAULT_RULES)
    if path:
        p = Path(path)
        if p.exists():
            rules.update(json.loads(p.read_text(encoding="utf-8")))
    return rules


def _ts(e: dict[str, Any]) -> float:
    try:
        return float(e.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0


def analyze(events: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    n = len(events)
    add(
        "event_count",
        "pass" if n >= rules.get("min_events", 1000) else "fail",
        f"{n} events (min {rules.get('min_events', 1000)})",
    )

    missing_keys = 0
    for e in events:
        if any(k not in e for k in ("ph", "name", "ts")):
            missing_keys += 1
    add(
        "required_fields",
        "pass" if missing_keys == 0 else "fail",
        f"{missing_keys} events missing ph/name/ts",
    )

    # process inventory via M events
    proc_names: dict[Any, str] = {}
    proc_labels: dict[Any, str] = {}
    for e in events:
        if e.get("ph") == "M":
            args = e.get("args") or {}
            if e.get("name") == "process_name":
                proc_names[e.get("pid")] = args.get("name", "")
            elif e.get("name") == "process_labels":
                proc_labels[e.get("pid")] = args.get("labels", "")

    found_procs = set(proc_names.values())

    def _proc_present(expected: str) -> bool:
        # tolerant: exact match, or case-insensitive containment (process names
        # vary across CANN/torch_npu versions, e.g. "CANN" vs "cannhost")
        if expected in found_procs:
            return True
        low = expected.lower()
        return any(low in f.lower() for f in found_procs if f)

    for expected in rules.get("expect_processes", []):
        add(
            f"process:{expected}",
            "pass" if _proc_present(expected) else "fail",
            f"process_name={sorted(found_procs)}",
        )

    # Overlap Analysis tracks (thread names under process 'Overlap Analysis')
    overlap_pids = {pid for pid, name in proc_names.items() if name == "Overlap Analysis"}
    overlap_tracks: set[str] = set()
    for e in events:
        if e.get("ph") == "M" and e.get("name") == "thread_name" and e.get("pid") in overlap_pids:
            overlap_tracks.add((e.get("args") or {}).get("name", ""))
    for track in rules.get("expect_overlap_tracks", []):
        add(
            f"overlap_track:{track}",
            "pass" if track in overlap_tracks else "fail",
            f"tracks={sorted(overlap_tracks)}",
        )

    cats = Counter(e.get("cat") for e in events)
    for expected in rules.get("expect_categories", []):
        add(
            f"category:{expected}",
            "pass" if cats.get(expected, 0) > 0 else "fail",
            f"counts={dict(cats)}",
        )

    # window span (exclude metadata events, which carry ts=0)
    timed = [e for e in events if e.get("ph") != "M" and _ts(e) != 0]
    if timed:
        span_us = max(_ts(e) for e in timed) - min(_ts(e) for e in timed)
        span_ms = span_us / 1000.0
    else:
        span_ms = 0.0
    win = rules.get("window_ms", {})
    target = float(win.get("target", 3000))
    tol = float(win.get("tolerance", 1500))
    ok_window = abs(span_ms - target) <= tol
    add(
        "window_span",
        "pass" if ok_window else "warn",
        f"span={span_ms:.1f}ms target={target:.0f}ms±{tol:.0f}ms",
    )

    # Node@launch presence (Top-N source)
    node_launch = sum(1 for e in events if e.get("name") == "Node@launch")
    if rules.get("require_node_launch", True):
        add(
            "node_launch",
            "pass" if node_launch > 0 else "fail",
            f"{node_launch} Node@launch events",
        )

    # flow balance s/f
    if rules.get("require_flow_balance", True):
        flow_s = sum(1 for e in events if e.get("ph") == "s")
        flow_f = sum(1 for e in events if e.get("ph") == "f")
        balanced = flow_s > 0 and abs(flow_s - flow_f) <= max(2, int(0.01 * max(flow_s, flow_f)))
        add(
            "flow_balance",
            "pass" if balanced else "warn",
            f"s={flow_s} f={flow_f}",
        )

    counters = [e for e in events if e.get("ph") == "C"]
    if rules.get("warn_missing_counter", True):
        add(
            "counter_events",
            "pass" if counters else "warn",
            f"{len(counters)} counter events (AI Core Freq etc.)",
        )

    # memory activity proxy sources (used by report memory chart)
    mem_proxy = sum(
        1
        for e in events
        if e.get("ph") == "X"
        and (
            (e.get("name") or "").startswith("Memcpy")
            or e.get("name") in ("empty_tensor", "MEMCPY_ASYNC", "acl_memcpy_host_to_device", "acl_memcpy_device_to_host")
        )
    )
    add(
        "memory_proxy_events",
        "pass" if mem_proxy > 0 else "warn",
        f"{mem_proxy} memcpy/empty_tensor events (memory activity proxy)",
    )

    status_rank = {"pass": 0, "warn": 1, "fail": 2}
    worst = max((status_rank.get(c["status"], 2) for c in checks), default=2)
    overall = {0: "pass", 1: "warn", 2: "fail"}[worst]

    return {
        "overall": overall,
        "summary": {
            "event_count": n,
            "processes": proc_names,
            "process_labels": proc_labels,
            "categories": dict(cats),
            "span_ms": span_ms,
            "node_launch": node_launch,
            "memory_proxy_events": mem_proxy,
        },
        "checks": checks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace-view", default="trace_view.json")
    ap.add_argument("--rules", default="", help="optional completeness_rules.json")
    ap.add_argument("--out", default="completeness_report.json")
    args = ap.parse_args()

    tv = Path(args.trace_view).expanduser().resolve()
    if not tv.exists():
        print(json.dumps({"error": f"trace_view not found: {tv}"}), file=sys.stderr)
        return 2

    with tv.open(encoding="utf-8") as f:
        events = json.load(f)
    if not isinstance(events, list):
        print(json.dumps({"error": "trace_view.json must be a JSON array of events"}), file=sys.stderr)
        return 2

    rules = load_rules(args.rules or None)
    report = analyze(events, rules)
    out = Path(args.out).expanduser().resolve()
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"out": str(out), "overall": report["overall"], "summary": report["summary"], "checks": report["checks"]}, ensure_ascii=False, indent=2))
    return 0 if report["overall"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
