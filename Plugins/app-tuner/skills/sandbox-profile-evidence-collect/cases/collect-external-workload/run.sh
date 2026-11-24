#!/usr/bin/env bash
set -euo pipefail
case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
skill_dir=$(cd "$case_dir/../.." && pwd)
entry="$skill_dir/scripts/run.sh"
legacy_entry="$skill_dir/../sandbox-timeline-profile/scripts/run_c50_profile.sh"
workflow_entry="$skill_dir/../sandbox-timeline-profile/scripts/run_profile_workflow.sh"
fixtures="$skill_dir/../sandbox-timeline-profile/cases/analyze-mock-logs/fixtures"
results_dir="$case_dir/results"
rm -rf "$results_dir"
mkdir -p "$results_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
log_dir="$work/logs"
space_log_dir="$work/logs with spaces"
space_api_log="$space_log_dir/CubeAPI/cube-api-main.log"
fake_bin="$work/bin"
fake_tools="$work/tools"
mkdir -p "$fake_bin" "$fake_tools"

python3 - "$log_dir" "$space_log_dir" <<'PY'
from pathlib import Path
import sys
paths = [
    "Cubelet/Cubelet-req.log", "Cubelet/Cubelet-stat.log",
    "CubeShim/cube-shim-req.log", "CubeShim/cube-shim-stat.log",
    "CubeVmm/vmm.log", "CubeMaster/cubemaster-req.log", "CubeAPI/cube-api-main.log",
]
for root_arg in sys.argv[1:]:
    root = Path(root_arg)
    for index, relative in enumerate(paths):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"before-{index}\n", encoding="utf-8")
PY

cat >"$work/workload.py" <<'PY'
import json
from pathlib import Path
import sys
log_root = Path(sys.argv[1])
mode = sys.argv[2]
argv_out = Path(sys.argv[3])
trigger_file = sys.argv[4]
extra = sys.argv[5:]
paths = [
    "Cubelet/Cubelet-req.log", "Cubelet/Cubelet-stat.log",
    "CubeShim/cube-shim-req.log", "CubeShim/cube-shim-stat.log",
    "CubeVmm/vmm.log", "CubeMaster/cubemaster-req.log", "CubeAPI/cube-api-main.log",
]
argv_out.parent.mkdir(parents=True, exist_ok=True)
argv_out.write_text(json.dumps(extra) + "\n", encoding="utf-8")
if trigger_file != "-":
    Path(trigger_file).write_text("STARTUP_BEGIN name=profile-c50-n500 50 500\n", encoding="utf-8")
if mode == "rotate":
    rotated = log_root / "Cubelet/Cubelet-stat.log"
    replacement = rotated.with_name("rotated.tmp")
    replacement.write_text("rotated-full\n", encoding="utf-8")
    replacement.replace(rotated)
for index, relative in enumerate(paths):
    with (log_root / relative).open("a", encoding="utf-8") as handle:
        handle.write(f"after-{mode}-{index}\n")
if mode == "fail":
    raise SystemExit(23)
PY

cat >"$work/full-workload.py" <<'PY'
from pathlib import Path
import shutil
import sys

log_root = Path(sys.argv[1])
profile = Path(sys.argv[2])
fixtures = Path(sys.argv[3])
trigger_file = sys.argv[4]
mode = sys.argv[5]
startup = profile / "startup-latency"
if mode == "deliver":
    shutil.copytree(fixtures / "startup-latency", startup)
elif mode == "unpaired":
    startup.mkdir(parents=True)
    shutil.copyfile(
        fixtures / "startup-latency/profile-c50-n500.summary.json",
        startup / "profile-c50-n500.summary.json",
    )
fixture_logs = {
    "Cubelet/Cubelet-stat.log": "cubelet_stat-delta.log",
    "CubeShim/cube-shim-req.log": "shim_req-delta.log",
    "CubeShim/cube-shim-stat.log": "shim_stat-delta.log",
    "CubeMaster/cubemaster-req.log": "master-delta.log",
}
paths = [
    "Cubelet/Cubelet-req.log", "Cubelet/Cubelet-stat.log",
    "CubeShim/cube-shim-req.log", "CubeShim/cube-shim-stat.log",
    "CubeVmm/vmm.log", "CubeMaster/cubemaster-req.log", "CubeAPI/cube-api-main.log",
]
for relative in paths:
    fixture_name = fixture_logs.get(relative)
    if fixture_name:
        with (log_root / relative).open("a", encoding="utf-8") as handle:
            handle.write((fixtures / "evidence" / fixture_name).read_text(encoding="utf-8"))
if trigger_file != "-":
    Path(trigger_file).write_text("STARTUP_BEGIN name=profile-c50-n500 50 500\n", encoding="utf-8")
PY

cat >"$work/full-legacy-runner.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
python3 "$FULL_WORKLOAD_SCRIPT" "$LOG_DIR" "$WORKDIR" "$FIXTURES" "$WORKDIR/run.log" deliver
SH

cat >"$fake_bin/curl" <<'SH'
#!/usr/bin/env bash
if [[ -n "${CURL_FAIL_BEFORE_ONCE:-}" && ! -e "$CURL_FAIL_BEFORE_ONCE" ]]; then
  : >"$CURL_FAIL_BEFORE_ONCE"
  printf 'injected metrics-before failure\n' >&2
  exit 7
fi
printf 'fake_metric 1\n'
SH
cat >"$fake_bin/ps" <<'SH'
#!/usr/bin/env bash
if [[ " $* " == *" args "* ]]; then
  printf 'PID TID PPID PSR STAT %%CPU %%MEM COMMAND WCHAN ARGS\n1 1 0 0 S 0.0 0.0 fake - fake-workload --token process-snapshot-secret --password=also-secret\n'
else
  printf 'PID TID PPID PSR STAT %%CPU %%MEM COMMAND WCHAN\n1 1 0 0 S 0.0 0.0 fake-workload -\n'
fi
SH
cat >"$fake_bin/bpftrace" <<'SH'
#!/usr/bin/env bash
printf 'called\n' >>"${BPF_MARKER:?}"
exit 99
SH
cat >"$fake_tools/sample_c50_host.sh" <<'SH'
#!/usr/bin/env bash
printf 'uptime_s,user,nice,system,idle,iowait,irq,softirq,steal,ctxt,processes,procs_running,procs_blocked,intr,softirq_total,load1,runnable,threads,mem_available_kib,dirty_kib,writeback_kib,shim_count,vmm_processes\n' >"$1"
printf '1,0,0,0,1,0,0,0,0,1,1,1,0,1,1,0,1,1,1,0,0,0,0\n' >>"$1"
printf '2,1,0,0,2,0,0,0,0,2,2,1,0,2,2,0,1,1,1,0,0,0,0\n' >>"$1"
SH
cat >"$work/legacy-runner.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
python3 "$WORKLOAD_SCRIPT" "$LOG_DIR" append "$WORKDIR/legacy-argv.json" "$WORKDIR/run.log" "legacy arg"
SH
chmod +x "$fake_bin/curl" "$fake_bin/ps" "$fake_bin/bpftrace"   "$work/legacy-runner.sh" "$work/full-legacy-runner.sh"
export PATH="$fake_bin:$PATH"
export BPF_MARKER="$work/bpftrace-called"
export WORKLOAD_SCRIPT="$work/workload.py"
common=(--log-dir "$log_dir" --tools-dir "$fake_tools" --host-duration 0.01 --host-interval 0.01 --process-samples 1 --process-interval 0.01 --no-bpftrace)

bash "$entry" --out-dir "$work/success" "${common[@]}" -- \
  python3 "$work/workload.py" "$log_dir" append "$work/success-argv.json" - "arg with space" "semi;colon" --api-key fake-secret
bash "$entry" --out-dir "$work/rotation" "${common[@]}" -- \
  python3 "$work/workload.py" "$log_dir" rotate "$work/rotation-argv.json" -
set +e
bash "$entry" --out-dir "$work/failure" "${common[@]}" -- \
  python3 "$work/workload.py" "$log_dir" fail "$work/failure-argv.json" -
failure_rc=$?

bash "$entry" --out-dir "$work/spaces" "${common[@]}"   --log-dir "$space_log_dir" --api-log "$space_api_log" --   python3 "$work/workload.py" "$space_log_dir" append "$work/spaces-argv.json" -

set +e
CURL_FAIL_BEFORE_ONCE="$work/metrics-before-attempted"   bash "$entry" --out-dir "$work/metrics-before-failure" "${common[@]}" --   python3 "$work/workload.py" "$log_dir" append "$work/metrics-before-argv.json" -
metrics_before_rc=$?
CURL_FAIL_BEFORE_ONCE="$work/metrics-before-workload-attempted"   bash "$entry" --out-dir "$work/metrics-before-workload-failure" "${common[@]}" --   python3 "$work/workload.py" "$log_dir" fail "$work/metrics-before-workload-argv.json" -
metrics_before_workload_rc=$?
set -e

BASE_DIR="$work/base" OUT_DIR="$work/legacy" LOG_DIR="$log_dir" \
METRICS_URL="http://fake/metrics" RUNNER="$work/legacy-runner.sh" TOOLS_DIR="$fake_tools" \
TEMPLATE_ID="fake-template" API_KEY="fake-key" BENCH="$work/fake-bench" \
SHIM_BIN="$work/fake-shim" TASK_DIR="$work/fake-tasks" BPFTRACE_MODE=off \
HOST_DURATION=0.01 HOST_INTERVAL=0.01 PROCESS_SAMPLES=1 PROCESS_INTERVAL=0.01 \
  bash "$legacy_entry"

mkdir -p "$work/external-full/evidence"
printf '@vm_kernel[vcpu0,\nmigration_entry_wait]: 999\n' >"$work/external-full/evidence/bpftrace-profile.txt"
env -u REPORT_OUT_DIR bash "$workflow_entry"   --collect-external "$work/external-full" "${common[@]}" --   python3 "$work/full-workload.py" "$log_dir" "$work/external-full" "$fixtures" - deliver

set +e
env -u REPORT_OUT_DIR bash "$workflow_entry"   --collect-external "$work/external-missing" "${common[@]}" --   python3 "$work/full-workload.py" "$log_dir" "$work/external-missing" "$fixtures" - missing   >"$work/external-missing.log" 2>&1
external_missing_rc=$?
env -u REPORT_OUT_DIR bash "$workflow_entry"   --collect-external "$work/external-unpaired" "${common[@]}" --   python3 "$work/full-workload.py" "$log_dir" "$work/external-unpaired" "$fixtures" - unpaired   >"$work/external-unpaired.log" 2>&1
external_unpaired_rc=$?
mkdir -p "$work/external-stale/startup-latency"
printf 'old-sentinel\n' >"$work/external-stale/startup-latency/old.summary.json"
env -u REPORT_OUT_DIR bash "$workflow_entry"   --collect-external "$work/external-stale" "${common[@]}" --   python3 "$work/full-workload.py" "$log_dir" "$work/external-stale" "$fixtures" - deliver   >"$work/external-stale.log" 2>&1
external_stale_rc=$?
set -e

env -u REPORT_OUT_DIR FULL_WORKLOAD_SCRIPT="$work/full-workload.py" FIXTURES="$fixtures" BASE_DIR="$work/legacy-full-base" OUT_DIR="$work/legacy-full" LOG_DIR="$log_dir" METRICS_URL="http://fake/metrics" RUNNER="$work/full-legacy-runner.sh" TOOLS_DIR="$fake_tools" TEMPLATE_ID="fake-template" API_KEY="fake-key" BENCH="$work/fake-bench" SHIM_BIN="$work/fake-shim" TASK_DIR="$work/fake-tasks" BPFTRACE_MODE=off HOST_DURATION=0.01 HOST_INTERVAL=0.01 PROCESS_SAMPLES=1 PROCESS_INTERVAL=0.01   bash "$workflow_entry" --collect-legacy

mkdir -p "$work/isolated/scripts"
cp "$skill_dir/scripts/run.sh" "$work/isolated/scripts/run.sh"
set +e
bash "$work/isolated/scripts/run.sh" --help >"$work/isolated.log" 2>&1
shared_rc=$?
set -e

mkdir -p "$work/workflow-default/scripts"
cp "$skill_dir/../sandbox-timeline-profile/scripts/run_profile_workflow.sh" "$work/workflow-default/scripts/"
cat >"$work/workflow-default/scripts/run_c50_profile.sh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"$work/workflow-default/scripts/run_stage_analysis.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$2"
printf 'ok\n' >"$2/default-dir-ok"
SH
cat >"$work/workflow-default/scripts/gen_timeline.py" <<'PY'
# Fake renderer: this contract only checks workflow directory routing.
PY
cat >"$work/workflow-default/scripts/generate_excel_charts.py" <<'PY'
# Fake reporter: this contract only checks workflow directory routing.
PY
env -u OUT_DIR -u REPORT_OUT_DIR BASE_DIR="$work/workflow-base" \
  bash "$work/workflow-default/scripts/run_profile_workflow.sh" --collect-legacy >"$work/workflow-default.log"

WORK="$work" RESULTS_DIR="$results_dir" FAILURE_RC="$failure_rc" SHARED_RC="$shared_rc" METRICS_BEFORE_RC="$metrics_before_rc" METRICS_BEFORE_WORKLOAD_RC="$metrics_before_workload_rc" EXTERNAL_MISSING_RC="$external_missing_rc" EXTERNAL_UNPAIRED_RC="$external_unpaired_rc" EXTERNAL_STALE_RC="$external_stale_rc" python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ["WORK"])
checks = []
def check(name, condition): checks.append({"name": name, "pass": bool(condition)})
def manifest(mode): return json.loads((root / mode / "evidence/evidence-manifest.json").read_text(encoding="utf-8"))
success = manifest("success")
rotation = manifest("rotation")
failure = manifest("failure")
legacy = manifest("legacy")
check("successful evidence manifest", success["status"] == "success" and success["workload"]["exit_code"] == 0)
check("argv preserved without eval", json.loads((root / "success-argv.json").read_text()) == ["arg with space", "semi;colon", "--api-key", "fake-secret"] and success["workload"]["exec_mode"] == "direct_argv_no_eval")
delta = (root / "success/evidence/cubelet_stat-delta.log").read_text(encoding="utf-8")
check("delta excludes before and includes workload bytes", "before-" not in delta and "after-append-1" in delta)
check("manifest redacts sensitive workload arguments", success["workload"]["argv"][-2:] == ["--api-key", "<redacted>"] and "fake-secret" not in json.dumps(success))
check("all seven component deltas exist", len(list((root / "success/evidence").glob("*-delta.log"))) == 7)
check("host process and metrics captured", all((root / "success/evidence" / name).is_file() for name in ["host-samples.csv", "process-snapshot-0.txt", "metrics-before.prom", "metrics-after.prom"]))
rotated_delta = (root / "rotation/evidence/cubelet_stat-delta.log").read_text(encoding="utf-8")
rotation_entry = next(item for item in rotation["logs"] if item["name"] == "cubelet_stat")
check("rotation falls back to full copy with warning", rotation_entry["rotation_fallback"] and "rotated-full" in rotated_delta and "cubelet_stat" in (root / "rotation/evidence/log-rotation-warnings.txt").read_text())
check("external failure code propagated and marked", int(os.environ["FAILURE_RC"]) == 23 and failure["status"] == "failed" and failure["workload"]["exit_code"] == 23)
check("failed workload evidence retained", "after-fail-1" in (root / "failure/evidence/cubelet_stat-delta.log").read_text(encoding="utf-8"))
check("legacy runner compatibility retained", legacy["status"] == "success" and (root / "legacy/run.log").is_file() and json.loads((root / "legacy/legacy-argv.json").read_text()) == ["legacy arg"])
check("bpftrace not loaded by default", not (root / "bpftrace-called").exists() and (root / "success/evidence/bpftrace-skipped.txt").is_file())
check("missing shared implementation is blocked", int(os.environ["SHARED_RC"]) == 2 and "BLOCKED" in (root / "isolated.log").read_text(encoding="utf-8"))
check("legacy workflow derives default output from BASE_DIR", (root / "workflow-base/profile-results/default-dir-ok").is_file())
snapshot = (root / "success/evidence/process-snapshot-0.txt").read_text(encoding="utf-8")
check("process snapshots omit argv secrets and retain comm", "process-snapshot-secret" not in snapshot and "also-secret" not in snapshot and "--token" not in snapshot and "fake-workload" in snapshot and "ARGS" not in snapshot)
spaces = manifest("spaces")
space_root = str(root / "logs with spaces")
space_api = str(root / "logs with spaces/CubeAPI/cube-api-main.log")
check("space-containing log and API paths survive offsets and manifest", spaces["status"] == "success" and all(item["path"].startswith(space_root) for item in spaces["logs"]) and next(item for item in spaces["logs"] if item["name"] == "api")["path"] == space_api and "\t" + space_api + "\t" in (root / "spaces/evidence/log-offsets-before.txt").read_text(encoding="utf-8") and "after-append-6" in (root / "spaces/evidence/api-delta.log").read_text(encoding="utf-8"))
metrics_before = manifest("metrics-before-failure")
check("metrics-before failure records manifest and still runs workload", int(os.environ["METRICS_BEFORE_RC"]) == 1 and metrics_before["status"] == "failed" and metrics_before["metrics_before_exit_code"] == 7 and metrics_before["metrics_after_exit_code"] == 0 and metrics_before["workload"]["exit_code"] == 0 and (root / "metrics-before-failure/evidence/metrics-before-error.txt").is_file() and (root / "metrics-before-argv.json").is_file())
metrics_before_workload = manifest("metrics-before-workload-failure")
check("workload failure code outranks metrics-before failure", int(os.environ["METRICS_BEFORE_WORKLOAD_RC"]) == 23 and metrics_before_workload["status"] == "failed" and metrics_before_workload["metrics_before_exit_code"] == 7 and metrics_before_workload["workload"]["exit_code"] == 23 and (root / "metrics-before-workload-argv.json").is_file())
external_analysis = json.loads((root / "external-full/analysis.json").read_text(encoding="utf-8"))
check("complete external workflow analyzes renders and reports current case", external_analysis["case"] == "profile-c50-n500" and (root / "external-full/timeline.svg").is_file() and (root / "external-full/timeline-render.json").is_file() and (root / "external-full/report-manifest.json").is_file() and (root / "external-full/timing_report_profile-c50-n500.xlsx").is_file())
external_manifest = manifest("external-full")
check("stale bpftrace is removed before no-bpftrace collection and analysis", not (root / "external-full/evidence/bpftrace-profile.txt").exists() and "bpftrace-profile.txt" not in external_manifest["artifacts"] and external_analysis["bpftrace"]["migration_entry_wait_samples"] == 0 and (root / "external-full/evidence/bpftrace-skipped.txt").is_file())
check("external workflow blocks missing startup pair after retaining evidence", int(os.environ["EXTERNAL_MISSING_RC"]) != 0 and manifest("external-missing")["status"] == "success" and "must create" in (root / "external-missing.log").read_text(encoding="utf-8") and not (root / "external-missing/analysis.json").exists())
check("external workflow blocks an unpaired case summary", int(os.environ["EXTERNAL_UNPAIRED_RC"]) != 0 and "missing benchmark matching" in (root / "external-unpaired.log").read_text(encoding="utf-8") and not (root / "external-unpaired/analysis.json").exists())
check("external workflow rejects stale startup before collecting", int(os.environ["EXTERNAL_STALE_RC"]) == 2 and "refusing to reuse old startup-latency" in (root / "external-stale.log").read_text(encoding="utf-8") and not (root / "external-stale/evidence").exists() and (root / "external-stale/startup-latency/old.summary.json").read_text(encoding="utf-8") == "old-sentinel\n")
legacy_full_analysis = json.loads((root / "legacy-full/analysis.json").read_text(encoding="utf-8"))
check("complete legacy workflow remains compatible", legacy_full_analysis["case"] == "profile-c50-n500" and (root / "legacy-full/timeline.svg").is_file() and (root / "legacy-full/report-manifest.json").is_file() and (root / "legacy-full/timing_report_profile-c50-n500.xlsx").is_file())
passed = sum(item["pass"] for item in checks)
result = {"checks_total": len(checks), "checks_passed": passed,
          "failed_checks": [item for item in checks if not item["pass"]]}
result["success"] = result["checks_total"] == result["checks_passed"]
(Path(os.environ["RESULTS_DIR"]) / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(f"checks: {passed}/{len(checks)} passed")
raise SystemExit(0 if result["success"] else 1)
PY
