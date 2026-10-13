#!/usr/bin/env bash
# analyze-mock-logs 用例驱动：用合成 delta 日志跑 analyze_c50_profile.mjs 并断言产物
# 用法: bash run.sh
set -euo pipefail

case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
analyze_script="$case_dir/../../scripts/analyze_c50_profile.mjs"
results_dir="$case_dir/results"
mkdir -p "$results_dir"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r "$case_dir/fixtures/." "$work/"

node "$analyze_script" "$work" \
  --csv-out "$work/stage-latency.csv" \
  --details-out "$work/create-container-details.jsonl" \
  > "$work/analysis.json"

CASE_DIR="$case_dir" WORK="$work" RESULTS_DIR="$results_dir" python3 - <<'EOF'
import csv, json, os, sys

work = os.environ["WORK"]
results_dir = os.environ["RESULTS_DIR"]

with open(os.path.join(work, "stage-latency.csv")) as f:
    rows = {r["stage_id"]: r for r in csv.DictReader(f)}
with open(os.path.join(work, "analysis.json")) as f:
    analysis = json.load(f)

checks = []
def check(name, cond, detail=""):
    checks.append({"name": name, "pass": bool(cond), "detail": detail})

def approx(a, b, eps=1e-6):
    try:
        return abs(float(a) - float(b)) < eps
    except (TypeError, ValueError):
        return False

for sid in ["api.create", "cubelet.cubebox-service", "cubelet.sandbox-probe",
            "shim.CreatePodSandbox", "shim.CreateContainer",
            "shim.create-request-total", "master.cube-e2e"]:
    check(f"csv contains stage {sid}", sid in rows)

check("cubelet.cubebox-service avg == 110", approx(rows.get("cubelet.cubebox-service", {}).get("avg_ms"), 110),
      rows.get("cubelet.cubebox-service", {}).get("avg_ms"))
check("shim.CreateContainer avg == 40 (hotspot)", approx(rows.get("shim.CreateContainer", {}).get("avg_ms"), 40),
      rows.get("shim.CreateContainer", {}).get("avg_ms"))
check("shim.CreatePodSandbox avg == 43", approx(rows.get("shim.CreatePodSandbox", {}).get("avg_ms"), 43))
check("cubelet.cubebox-service parent == master.cube-e2e",
      rows.get("cubelet.cubebox-service", {}).get("parent_stage_id") == "master.cube-e2e")
check("shim.CreateContainer parent == shim.CreatePodSandbox",
      rows.get("shim.CreateContainer", {}).get("parent_stage_id") == "shim.CreatePodSandbox")

timings = analysis.get("create_container_detail", {}).get("direct_timings_ms", {})
check("create_request_total avg == 50ms",
      approx(timings.get("create_request_total", {}).get("avg"), 50),
      timings.get("create_request_total"))
check("vm_start_to_agent_ready avg == 15ms",
      approx(timings.get("vm_start_to_agent_ready", {}).get("avg"), 15))
check("sandbox_to_container_finish avg == 17.5ms",
      approx(timings.get("sandbox_to_container_finish", {}).get("avg"), 17.5))
check("benchmark successful == 2", analysis.get("benchmark", {}).get("successful") == 2)
check("master cube-e2e avg == 115", approx(analysis.get("cubemaster_stages", {}).get("cube-e2e", {}).get("avg"), 115))

passed = sum(1 for c in checks if c["pass"])
result = {
    "success": passed == len(checks),
    "checks_total": len(checks),
    "checks_passed": passed,
    "stages_in_csv": len(rows),
    "failed_checks": [c for c in checks if not c["pass"]],
}
with open(os.path.join(results_dir, "result.json"), "w") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print(f"checks: {passed}/{len(checks)} passed -> {results_dir}/result.json")
if result["failed_checks"]:
    print(json.dumps(result["failed_checks"], indent=2, ensure_ascii=False))
sys.exit(0 if result["success"] else 1)
EOF
