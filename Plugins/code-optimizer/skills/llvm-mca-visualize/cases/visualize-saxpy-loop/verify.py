#!/usr/bin/env python3
"""Contract checks for cases/visualize-saxpy-loop.

Usage: verify.py <out.html> <out.json> <result.json>

Validates that the HTML heatmap and the --json contract describe the same
issue trace (per-cell occupancy, rows, cells), that default options are
applied (30 iterations / timeline 6 -> 5 shown groups), and that removed
features (Chrome Trace text) and encoding corruption are absent.
"""
import json
import re
import sys

PREFIXES = ("tsv110unit", "hip10cunit", "hip08unit", "hip09unit", "hip10unit",
            "hip11unit", "hip12unit", "tsv110", "hip10c", "hip08", "hip09",
            "hip10", "hip11", "hip12", "hip", "unit")


def short(name):
    n = (name or "").lower()
    for p in PREFIXES:
        n = n.replace(p, "")
    return n.upper()


def main():
    html_path, json_path, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
    html = open(html_path, encoding="utf-8").read()
    data = json.load(open(json_path, encoding="utf-8"))

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    meta = data.get("meta") or {}
    tl = data.get("timeline") or {}
    it = data.get("issue_trace") or {}
    records = it.get("records") or []
    reserved = it.get("reserved") or []
    instructions = tl.get("instructions") or []
    rp = data.get("resource_pressure_per_iteration") or []
    total_cycles = tl.get("cycles_total") or 0

    # 1. Defaults and data source
    check("top-level success == true", data.get("success") is True, data.get("success"))
    check("degraded flag absent/false", not data.get("degraded"), data.get("degraded"))
    check("meta.timeline_ok true", meta.get("timeline_ok") is True, meta.get("timeline_ok"))
    check("meta.iterations == 30", meta.get("iterations") == 30, meta.get("iterations"))
    check("meta.view == all", data.get("view") == "all", data.get("view"))
    check("issue_trace present", bool(records), len(records))

    # 2. Iteration window: 30/6 -> shown 1..5, 10 instructions each
    shown = {int(r.get("Iteration", -1)) for r in records}
    tl_iters = {int(x.get("iter", -1)) for x in instructions}
    check("shown iteration set == timeline iteration set", shown == tl_iters,
          sorted(shown))
    check("shown iterations == 5", len(shown) == 5, len(shown))
    check("records == timeline instruction count",
          len(records) == len(instructions), (len(records), len(instructions)))

    # 3. Record well-formedness
    bad = [r for r in records
           if not (r.get("Resources") or [])
           or any(float(u.get("Cycles", 0)) <= 0 for u in r["Resources"])
           or int(r.get("Cycle", -1)) < 0
           or int(r.get("Cycle", -1)) >= total_cycles]
    check("all records have valid resources/cycles", not bad,
          "%d bad record(s)" % len(bad))

    keys = {(int(x.get("iter", -1)), int(x.get("index", -1))) for x in instructions}
    missing = [(r.get("Iteration"), r.get("Index")) for r in records
               if (int(r.get("Iteration", -1)), int(r.get("Index", -1))) not in keys]
    check("every record maps to a timeline row", not missing,
          "%d missing" % len(missing))

    # 4. HTML structure produced by default options
    check("5 iteration groups", html.count('class="iterg"') == 5,
          html.count('class="iterg"'))
    check("3 groups expanded", html.count('class="iterg" open') == 3,
          html.count('class="iterg" open'))
    check("legend has reserved chip", "组保留（单元未定）" in html)
    check("no U+FFFD", "\ufffd" not in html)
    check("no removed trace text", "Chrome" not in html and "Perfetto" not in html)

    # 5. Heatmap <-> JSON consistency
    rows = {short(u.get("Kind", "")) for r in records for u in r.get("Resources") or []}
    rows |= {short(m) for r in reserved for m in r.get("Members") or []}
    heat_labels = html.count('class="lbl" style="fill:')
    heat_units = set(re.findall(
        r'class="lbl" style="fill:[^"]*" title="[^"]*">([^<]*)</text>', html))
    check("heatmap row count == distinct units", heat_labels == len(rows),
          (heat_labels, len(rows)))
    check("heatmap row names match JSON units", heat_units == rows,
          sorted(heat_units ^ rows))

    solid = html.count('class="hcell" style="fill:')
    cycles_sum = sum(max(1, int(round(float(u.get("Cycles", 0)))))
                     for r in records for u in r.get("Resources") or [])
    check("solid cells == sum(cycles)", solid == cycles_sum, (solid, cycles_sum))

    rsv_cells = html.count('class="hcell rsv"')
    rsv_sum = sum(len(r.get("Members") or []) * max(1, int(round(float(r.get("Cycles", 0)))))
                  for r in reserved)
    check("reserved cells == members x cycles", rsv_cells == rsv_sum,
          (rsv_cells, rsv_sum))

    # 6. Pressure chart rows == resources with positive pressure
    bars = html.count('class="lbl" text-anchor="end">')
    positive = len([x for x in rp if float(x.get("pressure", 0)) > 0])
    check("pressure bars == positive-pressure resources", bars == positive,
          (bars, positive))
    check("summary.ipc > 0", float((data.get("summary") or {}).get("ipc", 0)) > 0,
          (data.get("summary") or {}).get("ipc"))

    failures = [c for c in checks if not c["ok"]]
    result = {
        "success": not failures,
        "checks_ran": len(checks),
        "checks_failures": len(failures),
        "failures": failures,
        "kpi": {
            "iterations": meta.get("iterations"),
            "ipc": (data.get("summary") or {}).get("ipc"),
            "cycles_per_iteration": (data.get("summary") or {}).get("cycles_per_iteration"),
            "records": len(records),
            "solid_cells": solid,
        },
    }
    json.dump(result, open(result_path, "w"), ensure_ascii=False, indent=2)
    for c in checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["name"]
              + ((" | " + c["detail"]) if c["detail"] and not c["ok"] else ""))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
