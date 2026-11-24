#!/usr/bin/env bash
set -euo pipefail
case_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
skill_dir=$(cd "$case_dir/../.." && pwd)
entry="$skill_dir/scripts/run.sh"
fixtures="$skill_dir/../sandbox-timeline-profile/cases/analyze-mock-logs/fixtures"
results_dir="$case_dir/results"
rm -rf "$results_dir"
mkdir -p "$results_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/good" "$work/bad" "$work/failed-manifest"
cp -r "$fixtures/." "$work/good/"
cp -r "$fixtures/." "$work/bad/"
cp -r "$fixtures/." "$work/failed-manifest/"
printf '{"status":"failed","workload":{"exit_code":23}}\n' \
  >"$work/failed-manifest/evidence/evidence-manifest.json"
bash "$entry" "$work/good" "$work/out"

printf '{broken-json\n' >"$work/bad/evidence/cubelet_stat-delta.log"
set +e
bash "$entry" "$work/bad" "$work/bad-out" >"$work/bad.stdout" 2>"$work/bad.stderr"
bad_rc=$?
bash "$entry" "$work/missing" "$work/missing-out" >"$work/missing.stdout" 2>"$work/missing.stderr"
missing_rc=$?
bash "$entry" "$work/failed-manifest" "$work/failed-manifest-out" \
  >"$work/failed-manifest.stdout" 2>"$work/failed-manifest.stderr"
failed_manifest_rc=$?
set -e

WORK="$work" RESULTS_DIR="$results_dir" BAD_RC="$bad_rc" MISSING_RC="$missing_rc" \
SKILL_DIR="$skill_dir" FAILED_MANIFEST_RC="$failed_manifest_rc" python3 - <<'PY'
import csv
import json
import os
from pathlib import Path

work = Path(os.environ["WORK"])
out = work / "out"
data = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
with (out / "stage-latency.csv").open(encoding="utf-8", newline="") as handle:
    rows = {row["stage_id"]: row for row in csv.DictReader(handle)}
checks = []
def check(name, condition):
    checks.append({"name": name, "pass": bool(condition)})

expected = {"analysis.json", "stage-latency.csv", "create-container-details.jsonl", "analysis-summary.md"}
check("complete four-file output", all((out / name).is_file() for name in expected))
check("shared analyzer computes cubebox-service avg", float(rows["cubelet.cubebox-service"]["avg_ms"]) == 110)
check("CreateContainer topology retained", rows["shim.CreateContainer"]["parent_stage_id"] == "shim.CreatePodSandbox")
check("details JSONL has two instances", len((out / "create-container-details.jsonl").read_text(encoding="utf-8").splitlines()) == 2)
check("summary preserves interpretation boundaries", "不可直接相加" in (out / "analysis-summary.md").read_text(encoding="utf-8"))
check("published paths contain no temporary directory", ".stage-analysis" not in json.dumps(data["generated_files"]))
check("bad JSONL fails with source location", int(os.environ["BAD_RC"]) != 0 and "invalid JSONL" in (work / "bad.stderr").read_text(encoding="utf-8"))
check("bad input publishes no partial analysis", not (work / "bad-out" / "analysis.json").exists())
check("missing profile layout is blocked", int(os.environ["MISSING_RC"]) == 2 and "BLOCKED" in (work / "missing.stderr").read_text(encoding="utf-8"))
check("failed evidence manifest blocks analyzer with actionable error", int(os.environ["FAILED_MANIFEST_RC"]) != 0 and "status must be success" in (work / "failed-manifest.stderr").read_text(encoding="utf-8") and (work / "failed-manifest/evidence/evidence-manifest.json").is_file())
check("failed evidence publishes no analysis products", not (work / "failed-manifest-out").exists())
skill_dir = Path(os.environ["SKILL_DIR"])
skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
check("source interpretation link is relative and resolves", "../sandbox-timeline-profile/references/interpretation.md" in skill_text and (skill_dir / "../sandbox-timeline-profile/references/interpretation.md").is_file())
passed = sum(item["pass"] for item in checks)
result = {"success": passed == len(checks), "checks_total": len(checks), "checks_passed": passed,
          "failed_checks": [item for item in checks if not item["pass"]]}
(Path(os.environ["RESULTS_DIR"]) / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(f"checks: {passed}/{len(checks)} passed")
raise SystemExit(0 if result["success"] else 1)
PY
