#!/usr/bin/env bash
# analyze-mock-logs 用例驱动：用合成 delta 日志跑 analyze_rollback_profile.mjs 并断言产物
# 用法: bash run.sh
set -euo pipefail

case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
analyze_script="$case_dir/../../scripts/analyze_rollback_profile.mjs"
results_dir="$case_dir/results"
mkdir -p "$results_dir"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -r "$case_dir/fixtures/." "$work/"

node "$analyze_script" "$work" \
  --csv-out "$work/stage-latency.csv" \
  --details-out "$work/rollback-details.jsonl" \
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

for sid in ["api.rollback", "master.snapshot-rollback", "cubelet.rollback-service",
            "shim.RollbackSnapshot", "shim.RestoreVm", "hypervisor.restore-memory",
            "shim.DeleteVm", "shim.ResetVm", "shim.ConnectAgent"]:
    check(f"csv contains stage {sid}", sid in rows)

check("cubelet.rollback-service avg == 55", approx(rows.get("cubelet.rollback-service", {}).get("avg_ms"), 55),
      rows.get("cubelet.rollback-service", {}).get("avg_ms"))
check("shim.RestoreVm avg == 23 (hotspot)", approx(rows.get("shim.RestoreVm", {}).get("avg_ms"), 23),
      rows.get("shim.RestoreVm", {}).get("avg_ms"))
check("shim.RollbackSnapshot avg == 43", approx(rows.get("shim.RollbackSnapshot", {}).get("avg_ms"), 43))
check("hypervisor.restore-memory avg == 20", approx(rows.get("hypervisor.restore-memory", {}).get("avg_ms"), 20))
check("cubelet.rollback-service parent == master.snapshot-rollback",
      rows.get("cubelet.rollback-service", {}).get("parent_stage_id") == "master.snapshot-rollback")
check("shim.RestoreVm parent == shim.RollbackSnapshot",
      rows.get("shim.RestoreVm", {}).get("parent_stage_id") == "shim.RollbackSnapshot")
check("hypervisor.restore-memory parent == shim.RestoreVm",
      rows.get("hypervisor.restore-memory", {}).get("parent_stage_id") == "shim.RestoreVm")

timings = analysis.get("rollback_detail", {}).get("direct_timings_ms", {})
check("rollback_request_total avg == 55ms",
      approx(timings.get("rollback_request_total", {}).get("avg"), 55),
      timings.get("rollback_request_total"))
check("vm_downtime avg == 44ms",
      approx(timings.get("vm_downtime", {}).get("avg"), 44))
check("delete_vm avg == 9ms",
      approx(timings.get("delete_vm", {}).get("avg"), 9))
check("benchmark successful == 2", analysis.get("benchmark", {}).get("successful") == 2)
check("benchmark wall_ms avg == 70", approx(analysis.get("benchmark", {}).get("wall_ms", {}).get("avg"), 70))
check("master snapshot-rollback avg == 68", approx(analysis.get("cubemaster_stages", {}).get("snapshot-rollback", {}).get("avg"), 68))

# 同一沙箱多次回滚：明细按 rollback attempt 分段，RollbackSnapshot 耗时按尝试序配对
rb_detail = analysis.get("rollback_detail", {})
check("rollback attempt count == 3 (sb-rb-001 rolled back twice)",
      rb_detail.get("rollback_attempt_count") == 3, rb_detail.get("rollback_attempt_count"))
check("rollback instance count == 2", rb_detail.get("instance_count") == 2,
      rb_detail.get("instance_count"))

with open(os.path.join(work, "rollback-details.jsonl")) as f:
    detail_rows = [json.loads(line) for line in f if line.strip()]
att1 = next((d for d in detail_rows
             if d.get("instance_id") == "sb-rb-001" and d.get("rollback_attempt") == 1), None)
att2 = next((d for d in detail_rows
             if d.get("instance_id") == "sb-rb-001" and d.get("rollback_attempt") == 2), None)
check("sb-rb-001 attempt 1 exists", att1 is not None)
check("sb-rb-001 attempt 2 exists (second rollback of same sandbox)", att2 is not None)
check("sb-rb-001 attempt 2 rollback_request_total == 55ms",
      att2 is not None and approx(att2.get("rollback_request_total_ms"), 55),
      None if att2 is None else att2.get("rollback_request_total_ms"))
check("sb-rb-001 attempt 2 vm_downtime == 44ms",
      att2 is not None and approx(att2.get("vm_downtime_ms"), 44),
      None if att2 is None else att2.get("vm_downtime_ms"))
check("sb-rb-001 attempt 1 pairs RollbackSnapshot cost 40",
      att1 is not None and approx(att1.get("shim_rollback_snapshot_ms"), 40),
      None if att1 is None else att1.get("shim_rollback_snapshot_ms"))
check("sb-rb-001 attempt 2 has no RollbackSnapshot cost (paired by attempt order)",
      att2 is not None and att2.get("shim_rollback_snapshot_ms") is None,
      None if att2 is None else att2.get("shim_rollback_snapshot_ms"))

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
