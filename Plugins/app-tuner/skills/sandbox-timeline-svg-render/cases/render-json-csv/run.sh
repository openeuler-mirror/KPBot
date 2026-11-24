#!/usr/bin/env bash
set -euo pipefail
case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
skill_dir=$(cd "$case_dir/../.." && pwd)
entry="$skill_dir/scripts/run.sh"
results_dir="$case_dir/results"
rm -rf "$results_dir"
mkdir -p "$results_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

WORK="$work" python3 - <<'PY'
import csv
import json
import os
from pathlib import Path
root = Path(os.environ["WORK"])
(root / "analysis.json").write_text(json.dumps({"case": "mock-svg", "benchmark": {}, "correlated_instances": 2}) + "\n", encoding="utf-8")
fields = ["depth", "component", "stage_id", "stage_name", "parent_stage_id", "avg_ms", "notes"]
rows = [
    (0, "API", "api.create", "Create API", "", 120, ""),
    (1, "CubeMaster", "master.cube-e2e", "cube-e2e", "api.create", 110, ""),
    (2, "Cubelet", "cubelet.cubebox-service", "cubebox-service", "master.cube-e2e", 100, ""),
    (3, "Cubelet", "cubelet.cubebox", "cubebox", "cubelet.cubebox-service", 80, ""),
    (4, "Cubelet", "cubelet.sandbox-start", "sandbox-start", "cubelet.cubebox", 50, ""),
    (5, "Cubelet", "cubelet.sandbox-create", "sandbox-create", "cubelet.sandbox-start", 45, ""),
    (6, "CubeShim", "shim.CreatePodSandbox", "CreatePodSandbox", "cubelet.sandbox-create", 43, ""),
    (7, "CubeShim", "shim.LaunchVmm", "LaunchVmm", "shim.CreatePodSandbox", 5, ""),
    (7, "CubeShim", "shim.CreateContainer", "CreateContainer", "shim.CreatePodSandbox", 40, ""),
]
with (root / "stages.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(fields)
    writer.writerows(rows)
with (root / "negative.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(fields)
    writer.writerow((0, "API", "api.create", "bad", "", -1, ""))
with (root / "unknown.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(fields)
    writer.writerow((0, "API", "unknown.stage", "unknown", "", 10, ""))
(root / "bad.json").write_text("{bad\n", encoding="utf-8")
(root / "blocker").mkdir()
(root / "blocker" / "openpyxl.py").write_text("raise RuntimeError('Excel import forbidden on JSON/CSV path')\n", encoding="utf-8")
PY

PYTHONPATH="$work/blocker" bash "$entry" \
  --analysis "$work/analysis.json" --csv "$work/stages.csv" \
  --output "$work/out/timeline.svg" --metadata "$work/out/timeline-render.json"

set +e
bash "$entry" --analysis "$work/bad.json" --csv "$work/stages.csv" \
  --output "$work/invalid-json/timeline.svg" >"$work/invalid-json.log" 2>&1
bad_json_rc=$?
bash "$entry" --analysis "$work/analysis.json" --csv "$work/negative.csv" \
  --output "$work/invalid-csv/timeline.svg" >"$work/invalid-csv.log" 2>&1
bad_csv_rc=$?
bash "$entry" --analysis "$work/analysis.json" --csv "$work/unknown.csv" \
  --output "$work/no-stages/timeline.svg" >"$work/no-stages.log" 2>&1
no_stages_rc=$?
set -e

WORK="$work" RESULTS_DIR="$results_dir" BAD_JSON_RC="$bad_json_rc" BAD_CSV_RC="$bad_csv_rc" NO_STAGES_RC="$no_stages_rc" ENTRY="$entry" python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
root = Path(os.environ["WORK"])
out = root / "out"
svg = (out / "timeline.svg").read_text(encoding="utf-8")
metadata = json.loads((out / "timeline-render.json").read_text(encoding="utf-8"))
checks = []
def check(name, condition): checks.append({"name": name, "pass": bool(condition)})
check("valid XML SVG", ET.fromstring(svg).tag.endswith("svg"))
check("real average and topology rendered", "40.0ms" in svg and "CreateContainer" in svg)
check("metadata marks direct non-Excel path", metadata["source_format"] == "analysis-json+stage-csv" and metadata["excel_dependency"] is False)
check("metadata counts rendered bars", metadata["rendered_bar_count"] == 9 and metadata["case"] == "mock-svg")
check("text files end with newline", svg.endswith("\n") and (out / "timeline-render.json").read_bytes().endswith(b"\n"))
check("invalid JSON rejected without SVG", int(os.environ["BAD_JSON_RC"]) == 2 and not (root / "invalid-json/timeline.svg").exists())
check("negative duration rejected without SVG", int(os.environ["BAD_CSV_RC"]) == 2 and not (root / "invalid-csv/timeline.svg").exists())
check("unknown-only topology rejected as empty", int(os.environ["NO_STAGES_RC"]) == 2 and not (root / "no-stages/timeline.svg").exists())
entry = os.environ["ENTRY"]
collisions = root / "collisions"
collisions.mkdir()
analysis = root / "analysis.json"
csv_path = root / "stages.csv"

def run_collision(name, arguments, sentinels, forbidden=None):
    before = {path: path.read_bytes() for path in sentinels}
    completed = subprocess.run(["bash", entry, *map(str, arguments)], text=True, capture_output=True)
    preserved = all(path.read_bytes() == content for path, content in before.items())
    no_write = forbidden is None or not forbidden.exists()
    check(name, completed.returncode == 2 and "path collision" in completed.stderr and preserved and no_write)

same_output = collisions / "same.svg"
same_output.write_text("svg-metadata-sentinel\n", encoding="utf-8")
run_collision(
    "metadata and SVG collision preserves sentinel",
    ["--analysis", analysis, "--csv", csv_path, "--output", same_output, "--metadata", same_output],
    [same_output],
)
run_collision(
    "metadata resolves to analysis input without writes",
    ["--analysis", analysis, "--csv", csv_path, "--output", root / "new-tree/timeline.svg", "--metadata", collisions / "../analysis.json"],
    [analysis],
    root / "new-tree",
)
run_collision(
    "metadata and CSV collision preserves input",
    ["--analysis", analysis, "--csv", csv_path, "--output", collisions / "csv-collision.svg", "--metadata", csv_path],
    [csv_path],
)
run_collision(
    "SVG and analysis collision preserves input",
    ["--analysis", analysis, "--csv", csv_path, "--output", analysis, "--metadata", collisions / "analysis-collision.json"],
    [analysis],
)
run_collision(
    "SVG and CSV collision preserves input",
    ["--analysis", analysis, "--csv", csv_path, "--output", csv_path, "--metadata", collisions / "csv-output-collision.json"],
    [csv_path],
)
shared_input = collisions / "shared-input"
shared_input.write_text("shared-input-sentinel\n", encoding="utf-8")
run_collision(
    "duplicate input aliases are rejected before parsing",
    ["--analysis", shared_input, "--csv", shared_input, "--output", collisions / "shared-input.svg"],
    [shared_input],
)
legacy_input = collisions / "legacy.xlsx"
legacy_input.write_text("legacy-sentinel\n", encoding="utf-8")
run_collision("legacy Excel input and SVG collision preserves sentinel", [legacy_input, legacy_input], [legacy_input])
passed = sum(item["pass"] for item in checks)
result = {"success": passed == len(checks), "checks_total": len(checks), "checks_passed": passed,
          "failed_checks": [item for item in checks if not item["pass"]]}
(Path(os.environ["RESULTS_DIR"]) / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(f"checks: {passed}/{len(checks)} passed")
raise SystemExit(0 if result["success"] else 1)
PY
