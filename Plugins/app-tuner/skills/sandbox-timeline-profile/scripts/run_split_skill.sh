#!/usr/bin/env bash
# Shared dispatcher used by the four focused sandbox profile Skills.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
command_name=${1:-}

usage() {
  cat <<'EOF'
Usage: run_split_skill.sh {evidence|analyze|svg|report|workflow} [arguments]

This is the shared implementation entry. Call the focused Skills' scripts/run.sh
wrappers for their stable command-specific contracts.
EOF
}

case "$command_name" in
  evidence)
    shift
    exec bash "$script_dir/collect_evidence.sh" "$@"
    ;;
  analyze)
    shift
    exec bash "$script_dir/run_stage_analysis.sh" "$@"
    ;;
  svg)
    shift
    exec python3 "$script_dir/gen_timeline.py" "$@"
    ;;
  report)
    shift
    exec python3 "$script_dir/generate_excel_charts.py" "$@"
    ;;
  workflow)
    shift
    exec bash "$script_dir/run_profile_workflow.sh" "$@"
    ;;
  -h|--help|'')
    usage
    [[ "$command_name" == -h || "$command_name" == --help ]]
    ;;
  *)
    printf 'ERROR: unknown shared command: %s\n' "$command_name" >&2
    usage >&2
    exit 2
    ;;
esac
