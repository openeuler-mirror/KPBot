#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
skill_dir=$(cd "$case_dir/../.." && pwd)
entry="$skill_dir/scripts/run.sh"
shared="$skill_dir/../sandbox-timeline-profile/scripts"
results_dir="$case_dir/results"
rm -rf "$results_dir"
mkdir -p "$results_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/legacy-profile"

WORK="$work" python3 - <<'PY'
import csv
import json
import os
from pathlib import Path
root = Path(os.environ["WORK"])
def stats(avg):
    return {"count": 2, "avg": avg, "min": avg - 1, "p50": avg, "p95": avg + 1, "max": avg + 1}
data = {
    "case": "mock-report",
    "benchmark": {"successful": 2, "errors": 0, "success_rate": 1.0, "throughput_qps": 2.0,
                  "total_time_s": 1.0, "create": {"min": 90, "avg": 100, "std": 2, "p50": 100,
                  "p90": 105, "p95": 110, "p99": 115, "max": 120}},
    "benchmark_blocks": [{"sequence": "1-2", "count": 2, "avg": 100, "min": 90, "p50": 100, "p95": 110, "max": 120}],
    "cubelet_stages": {"cubebox-service": stats(100)},
    "shim_stages": {"CreateContainer": stats(40), "CreatePodSandbox": stats(43)},
    "create_container_detail": {"direct_timings_ms": {"create_request_total": stats(50), "unobserved": None}, "path_counts": {"restore": 2}},
    "cubemaster_stages": {"cube-e2e": stats(115), "all-probe": None},
    "host_summary": {"sample_count": 3, "max_runnable": 2, "max_procs_running": 2,
                     "max_procs_blocked": 0, "max_threads": 100, "max_shims": 2,
                     "max_iowait_pct": 1.0, "max_system_pct": 10.0, "max_ctxt_per_s": 1000,
                     "max_processes_per_s": 2},
    "host_intervals": [
        {"end_s": 0.1, "runnable": 1, "shim_count": 1, "busy_pct": 20, "system_pct": 5,
         "iowait_pct": 0.5, "ctxt_per_s": 500, "threads": 90, "procs_running": 1},
        {"end_s": 0.2, "runnable": 2, "shim_count": 2, "busy_pct": 30, "system_pct": 10,
         "iowait_pct": 1.0, "ctxt_per_s": 1000, "threads": 100, "procs_running": 2},
    ],
    "correlated_instances": 2,
}
text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
(root / "analysis.json").write_text(text, encoding="utf-8")
(root / "legacy-profile" / "analysis.json").write_text(text, encoding="utf-8")
fields = ["depth", "component", "stage_id", "stage_name", "parent_stage_id", "count",
          "avg_ms", "min_ms", "p50_ms", "p95_ms", "max_ms", "notes"]
with (root / "stage-latency.csv").open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(fields)
    writer.writerow([0, "API", "api.create", "Create API", "", 2, 120, 110, 120, 125, 130, ""])
PY

cat >"$work/bin/node" <<'SH'
#!/usr/bin/env bash
printf 'node must not be called\n' >>"${NODE_MARKER:?}"
exit 99
SH
chmod +x "$work/bin/node"
export PATH="$work/bin:$PATH"
export NODE_MARKER="$work/node-called"

bash "$entry" --analysis "$work/analysis.json" --output-dir "$work/report" --manifest "$work/report/report-manifest.json"
bash "$entry" "$work/legacy-profile" "$work/legacy-report"
set +e
bash "$entry" "$work/no-such-profile" "$work/missing-analysis-report" >"$work/missing-analysis.log" 2>&1
missing_analysis_rc=$?
set -e
python3 "$shared/export_to_excel.py" "$work/analysis.json" "$work/stage-latency.csv" "$work/compat/profile-results.xlsx"

WORK="$work" RESULTS_DIR="$results_dir" SHARED="$shared" MISSING_ANALYSIS_RC="$missing_analysis_rc" python3 - <<'PY'
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from openpyxl import load_workbook
root = Path(os.environ["WORK"])
shared = Path(os.environ["SHARED"])
checks = []
def check(name, condition): checks.append({"name": name, "pass": bool(condition)})
report = root / "report"
case = "mock-report"
expected_sheets = ["Bench总体", "分批时延", "Cubelet阶段", "Shim阶段", "详细子阶段", "CubeMaster阶段", "Host资源"]
wb = load_workbook(report / f"timing_report_{case}.xlsx", read_only=True)
check("complete seven-sheet Excel", wb.sheetnames == expected_sheets)
wb.close()
pngs = [report / f"stages_chart_{case}.png", report / f"blocks_timeline_{case}.png", report / f"host_resources_{case}.png"]
check("three non-empty PNG charts", all(path.read_bytes().startswith(b"\x89PNG\r\n") for path in pngs))
manifest = json.loads((report / "report-manifest.json").read_text(encoding="utf-8"))
digest = hashlib.sha256((root / "analysis.json").read_bytes()).hexdigest()
check("manifest proves existing analysis reuse", manifest["analysis_reused"] and not manifest["analyzer_invoked"] and manifest["analysis_sha256"] == digest and len(manifest["artifacts"]) == 4)
check("report never calls node analyzer", not (root / "node-called").exists())
check("legacy profile_dir consumes existing analysis", (root / f"legacy-report/timing_report_{case}.xlsx").is_file() and (root / "legacy-report/report-manifest.json").is_file())

spec = importlib.util.spec_from_file_location("report_module_for_missing_artifact", shared / "generate_excel_charts.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.generate_host_chart = lambda *args: None
broken_rc = module.main(["--analysis", str(root / "analysis.json"), "--output-dir", str(report), "--manifest", str(report / "report-manifest.json")])
check("missing generated Host PNG fails explicitly", broken_rc == 2)
check("missing artifact has no success manifest", not (report / "report-manifest.json").exists())
check("legacy missing analysis is blocked", int(os.environ["MISSING_ANALYSIS_RC"]) == 2 and "existing analysis is required" in (root / "missing-analysis.log").read_text(encoding="utf-8"))
compat = load_workbook(root / "compat/profile-results.xlsx", read_only=True)
check("lightweight compatibility exporter remains usable", compat.sheetnames == ["Stage Latency", "Summary"])
compat.close()
check("compatibility export also avoids analyzer", not (root / "node-called").exists())

def tree_snapshot(directory):
    if not directory.exists():
        return None
    files = {str(path.relative_to(directory)): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    directories = {str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_dir()}
    return files, directories

def report_collision(name, analysis_path, output_dir, manifest_path, protected):
    before = {path: path.read_bytes() for path in protected}
    before_tree = tree_snapshot(output_dir)
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        rc = module.main(["--analysis", str(analysis_path), "--output-dir", str(output_dir), "--manifest", str(manifest_path)])
    preserved = all(path.read_bytes() == content for path, content in before.items())
    check(name, rc == 2 and "path collision" in errors.getvalue() and preserved and tree_snapshot(output_dir) == before_tree)

collision_out = root / "manifest-analysis-collision"
report_collision(
    "manifest and analysis collision performs no mkdir or writes",
    root / "analysis.json", collision_out, root / "analysis.json", [root / "analysis.json"],
)
artifact_names = [
    f"timing_report_{case}.xlsx",
    f"stages_chart_{case}.png",
    f"blocks_timeline_{case}.png",
    f"host_resources_{case}.png",
]
for artifact_name in artifact_names:
    output_dir = root / f"manifest-artifact-{artifact_name}"
    output_dir.mkdir()
    artifact = output_dir / artifact_name
    artifact.write_text("manifest-artifact-sentinel\n", encoding="utf-8")
    manifest_alias = output_dir / ".." / output_dir.name / artifact_name
    report_collision(
        f"manifest and {artifact_name} collision preserves sentinel",
        root / "analysis.json", output_dir, manifest_alias, [artifact],
    )
for artifact_name in artifact_names:
    output_dir = root / f"analysis-artifact-{artifact_name}"
    output_dir.mkdir()
    artifact = output_dir / artifact_name
    artifact.write_bytes((root / "analysis.json").read_bytes())
    report_collision(
        f"analysis and {artifact_name} collision preserves source",
        artifact, output_dir, output_dir / "report-manifest.json", [artifact],
    )
passed = sum(item["pass"] for item in checks)
result = {"success": passed == len(checks), "checks_total": len(checks), "checks_passed": passed,
          "failed_checks": [item for item in checks if not item["pass"]]}
(Path(os.environ["RESULTS_DIR"]) / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(f"checks: {passed}/{len(checks)} passed")
if result["failed_checks"]:
    print(json.dumps(result["failed_checks"], ensure_ascii=False, indent=2))
raise SystemExit(0 if result["success"] else 1)
PY
