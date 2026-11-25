#!/usr/bin/env python3
"""Heatmap boundary: occupancy starting at C-1 must render only the visible part."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mca_visualize as mca  # noqa: E402


def main():
    data = {
        "timeline": {
            "ok": True,
            "cycles_total": 11,
            "instructions": [{"iter": 0, "index": 0, "text": "eor x0, x1, x2", "stages": {}}],
        },
        "resources": ["HIP12UnitS0"],
        "resource_pressure_per_iteration": [{"resource": "HIP12UnitS0", "pressure": 1.0}],
        "issue_trace": {
            "records": [{
                "Iteration": 0, "Index": 0, "Cycle": 10,
                "Resources": [{"Kind": "HIP12UnitS0", "Cycles": 2}],
            }],
            "reserved": [],
        },
    }
    html = mca._resource_timeline_svg(data)
    solid = html.count('class="hcell" style="fill:')
    start_c = dict(data)
    start_c["issue_trace"] = {
        "records": [{
            "Iteration": 0, "Index": 0, "Cycle": 11,
            "Resources": [{"Kind": "HIP12UnitS0", "Cycles": 2}],
        }],
        "reserved": [],
    }
    html_c = mca._resource_timeline_svg(start_c)
    checks = [
        ("C-1 + 2 cycles draws 1 cell", solid == 1, solid),
        ("start C draws no cell", 'class="hcell" style="fill:' not in html_c, ""),
    ]
    failed = 0
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
            print("     detail:", detail)
    print("%d/%d passed" % (len(checks) - failed, len(checks)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
