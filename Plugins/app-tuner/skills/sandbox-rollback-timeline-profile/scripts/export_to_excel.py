#!/usr/bin/env python3
"""Convert CubeSandbox rollback profiling results to Excel.

Usage:
    python export_to_excel.py <analysis.json> [<stage-latency.csv>] [output.xlsx]

    If stage-latency.csv is omitted, reads from the same directory as analysis.json.
"""

import csv, json, sys
from pathlib import Path
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

# ── config ──────────────────────────────────────────────
KEEP_COLS = [
    "depth", "component", "stage_id", "stage_name", "parent_stage_id",
    "count", "avg_ms", "min_ms", "p50_ms", "p95_ms", "max_ms", "notes",
]

COL_WIDTHS = {
    "depth": 6, "component": 12, "stage_id": 30, "stage_name": 26,
    "parent_stage_id": 30, "count": 6, "avg_ms": 10, "min_ms": 10,
    "p50_ms": 10, "p95_ms": 10, "max_ms": 10, "notes": 60,
}

# ── args ────────────────────────────────────────────────
if len(sys.argv) < 2:
    print("Usage: python export_to_excel.py ANALYSIS_JSON [STAGE_LATENCY_CSV] [OUTPUT.xlsx]")
    sys.exit(2)

analysis_path = Path(sys.argv[1])
csv_path = Path(sys.argv[2]) if len(sys.argv) > 2 else analysis_path.parent / "stage-latency.csv"
output_path = Path(sys.argv[3]) if len(sys.argv) > 3 else analysis_path.parent / "rollback-profile-results.xlsx"

# ── load data ───────────────────────────────────────────
with open(analysis_path, encoding="utf-8") as f:
    data = json.load(f)

rows = []
if csv_path.exists():
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
else:
    print(f"Warning: {csv_path} not found, stage-latency sheet will be empty")

# ── build workbook ──────────────────────────────────────
wb = Workbook()

# Sheet 1: Stage Latency
ws = wb.active
ws.title = "Stage Latency"
for ci, col in enumerate(KEEP_COLS, 1):
    ws.cell(row=1, column=ci, value=col)
for ri, row in enumerate(rows, 2):
    for ci, col in enumerate(KEEP_COLS, 1):
        val = row.get(col, "")
        if col == "count":
            try: val = int(float(val))
            except: pass
        elif col in ("avg_ms", "min_ms", "p50_ms", "p95_ms", "max_ms"):
            try: val = round(float(val), 2)
            except: pass
        elif col == "depth":
            try: val = int(val)
            except: pass
        ws.cell(row=ri, column=ci, value=val)
for ci, col in enumerate(KEEP_COLS, 1):
    ws.column_dimensions[get_column_letter(ci)].width = COL_WIDTHS.get(col, 12)
ws.freeze_panes = "A2"

# Sheet 2: Summary
ws2 = wb.create_sheet("Summary")
ws2.column_dimensions["A"].width = 30
ws2.column_dimensions["B"].width = 20
# 上游在缺 host 采样/全部轮次失败时会输出 null，消费端统一兜底
b = data.get("benchmark") or {}
wall = b.get("wall_ms") or {}
per = b.get("per_rollback_ms") or {}
host = data.get("host_summary") or {}

def stat_or_na(value, digits=2):
    return round(value, digits) if isinstance(value, (int, float)) else "n/a"

items = [
    ("Case", data.get("case", "")),
    ("Correlated Instances", data.get("correlated_instances", "")),
    ("Concurrency", b.get("concurrency", "")),
    ("Rounds", b.get("rounds", "")),
    ("Dirty MB", b.get("dirty_mb", "")),
    ("", ""),
    ("Errors", b.get("errors", "")),
    ("Success Rate", f"{(b.get('success_rate') or 0) * 100}%"),
    ("Throughput (RPS)", stat_or_na(b.get("throughput_rps"), 1)),
    ("Total Time (s)", stat_or_na(b.get("total_time_s"))),
    ("", ""),
    ("Batch Wall Avg (ms)", stat_or_na(wall.get("avg"))),
    ("Batch Wall P50 (ms)", stat_or_na(wall.get("p50"))),
    ("Batch Wall P95 (ms)", stat_or_na(wall.get("p95"))),
    ("Batch Wall Max (ms)", stat_or_na(wall.get("max"))),
    ("Per-Rollback Avg (ms)", stat_or_na(per.get("avg"))),
    ("Per-Rollback P50 (ms)", stat_or_na(per.get("p50"))),
    ("Per-Rollback P95 (ms)", stat_or_na(per.get("p95"))),
    ("Per-Rollback Max (ms)", stat_or_na(per.get("max"))),
    ("", ""),
    ("Max Runnable", host.get("max_runnable", "n/a")),
    ("Max Shims", host.get("max_shims", "n/a")),
    ("Max IOWait %", stat_or_na(host.get("max_iowait_pct"), 3)),
    ("Max System %", stat_or_na(host.get("max_system_pct"))),
    ("", ""),
    ("Rollback path counts", ""),
]
cd = data.get("rollback_detail") or {}
for path, cnt in cd.get("path_counts", {}).items():
    items.append((f"  {path}", cnt))
for metric, value in items:
    ws2.append([metric, value])

wb.save(output_path)
print(f"Saved: {output_path}  ({len(rows)} stage rows)")
