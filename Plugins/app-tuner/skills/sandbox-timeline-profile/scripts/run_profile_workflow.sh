#!/usr/bin/env bash
# Thin one-click workflow over evidence, analysis, SVG, and report stages.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
usage() {
  cat <<'EOF'
Usage:
  run_profile_workflow.sh PROFILE_DIR [OUTPUT_DIR]
  run_profile_workflow.sh --collect-legacy
  run_profile_workflow.sh --collect-external PROFILE_DIR [collector options] -- COMMAND [ARG ...]

Existing evidence is analyzed directly. --collect-legacy preserves the original
run_c50_profile.sh environment-variable benchmark flow; --collect-external wraps
an external argv workload. The external workload must write one matching CASE.json
and CASE.summary.json pair to PROFILE_DIR/startup-latency before it exits. Use a
PROFILE_DIR without an existing startup-latency directory for each external run.
Set REPORT_OUT_DIR to separate outputs from evidence.
EOF
}

mode=${1:-}
case "$mode" in
  -h|--help|'') usage; exit 0 ;;
  --collect-legacy)
    shift
    (($# == 0)) || { usage >&2; exit 2; }
    bash "$script_dir/run_c50_profile.sh"
    profile_dir=${OUT_DIR:-${BASE_DIR:?"--collect-legacy requires BASE_DIR or OUT_DIR"}/profile-results}
    out_dir=${REPORT_OUT_DIR:-$profile_dir}
    ;;
  --collect-external)
    shift
    profile_dir=${1:?"--collect-external requires PROFILE_DIR"}
    shift
    if [[ -e "$profile_dir/startup-latency" || -L "$profile_dir/startup-latency" ]]; then
      printf 'BLOCKED: refusing to reuse old startup-latency: %s\n' "$profile_dir/startup-latency" >&2
      printf 'Use a new PROFILE_DIR (or archive/remove the old directory) so this run cannot analyze stale benchmark data.\n' >&2
      exit 2
    fi
    bash "$script_dir/collect_evidence.sh" --out-dir "$profile_dir" "$@"
    python3 - "$profile_dir" <<'PY'
import json
from pathlib import Path
import sys

profile = Path(sys.argv[1])
startup = profile / "startup-latency"
hint = ("The external workload must create exactly one matching "
        "PROFILE_DIR/startup-latency/<case>.summary.json and <case>.json pair "
        "for this run before it exits; evidence has been retained.")

def blocked(message):
    print(f"BLOCKED: {message}", file=sys.stderr)
    print(hint, file=sys.stderr)
    raise SystemExit(2)

if not startup.is_dir():
    blocked(f"startup-latency was not created by this workload: {startup}")
summaries = sorted(path for path in startup.glob("*.summary.json") if path.is_file())
if len(summaries) != 1:
    blocked(f"expected one case summary, found {len(summaries)} in {startup}")
summary_path = summaries[0]
case = summary_path.name[:-len(".summary.json")]
benchmark_path = startup / f"{case}.json"
if not case or not benchmark_path.is_file():
    blocked(f"missing benchmark matching {summary_path.name}: {benchmark_path}")
try:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    blocked(f"cannot read case summary {summary_path}: {error}")
if isinstance(summary, dict) and summary.get("case") not in (None, case):
    blocked(f"summary case {summary.get('case')!r} does not match filename case {case!r}")
print(f"External startup evidence accepted for case: {case}")
PY
    out_dir=${REPORT_OUT_DIR:-$profile_dir}
    ;;
  *)
    profile_dir=$mode
    if (($# > 2)); then usage >&2; exit 2; fi
    out_dir=${2:-$profile_dir}
    ;;
esac

bash "$script_dir/run_stage_analysis.sh" "$profile_dir" "$out_dir"
python3 "$script_dir/gen_timeline.py" \
  --analysis "$out_dir/analysis.json" \
  --csv "$out_dir/stage-latency.csv" \
  --output "$out_dir/timeline.svg" \
  --metadata "$out_dir/timeline-render.json"
python3 "$script_dir/generate_excel_charts.py" \
  --analysis "$out_dir/analysis.json" \
  --output-dir "$out_dir" \
  --manifest "$out_dir/report-manifest.json"
printf 'Workflow complete: %s\n' "$out_dir"
