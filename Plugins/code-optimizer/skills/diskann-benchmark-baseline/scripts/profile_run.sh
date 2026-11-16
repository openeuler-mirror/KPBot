#!/usr/bin/env bash
set -uo pipefail

usage() {
  echo "Usage: profile_run.sh --out-dir DIR [--interval SEC] [--perf auto|required|off] -- command [args...]"
}

out_dir=""
interval="1"
perf_mode="auto"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out-dir|--interval|--perf)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      case "$1" in
        --out-dir) out_dir="$2" ;;
        --interval) interval="$2" ;;
        --perf) perf_mode="$2" ;;
      esac
      shift 2 ;;
    --) shift; break ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$out_dir" ] || [ "$#" -eq 0 ] || ! [[ "$interval" =~ ^[1-9][0-9]*$ ]]; then
  usage >&2; exit 2
fi
case "$perf_mode" in auto|required|off) ;; *) usage >&2; exit 2 ;; esac
mkdir -p "$out_dir" || exit 2
if [ -e "$out_dir/command.txt" ]; then
  echo "Output already contains a profile run; use a new directory" >&2; exit 2
fi
printf '%q ' "$@" > "$out_dir/command.txt"
printf '\n' >> "$out_dir/command.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$out_dir/start_time.txt"

# Preflight on a trivial command, never rerun the workload to hide a perf failure.
events="task-clock,cycles,instructions,branches,branch-misses,cache-references,cache-misses,context-switches,cpu-migrations,page-faults"
use_perf=false
if [ "$perf_mode" != off ] && command -v perf >/dev/null 2>&1; then
  if perf stat -e "$events" -o "$out_dir/perf-probe.txt" -- true 2> "$out_dir/perf-probe.stderr.txt"; then
    use_perf=true
  fi
fi
if [ "$use_perf" = true ]; then
  echo available > "$out_dir/perf-status.txt"
elif [ "$perf_mode" = required ]; then
  echo unavailable > "$out_dir/perf-status.txt"
  echo "Required perf counters unavailable; workload was not started" >&2
  echo 2 > "$out_dir/exit_code.txt"
  exit 2
else
  echo "$perf_mode-unavailable-or-disabled" > "$out_dir/perf-status.txt"
fi

iostat_pid=""
if command -v iostat >/dev/null 2>&1; then
  iostat -x -d "$interval" > "$out_dir/iostat.txt" 2>&1 &
  iostat_pid=$!
fi
cleanup() {
  if [ -n "$iostat_pid" ]; then
    kill "$iostat_pid" 2>/dev/null || true
    wait "$iostat_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$use_perf" = true ]; then
  perf stat -x, -o "$out_dir/perf-stat.csv" -e "$events" \
    -- "$@" > "$out_dir/stdout.txt" 2> "$out_dir/stderr.txt"
  status=$?
else
  "$@" > "$out_dir/stdout.txt" 2> "$out_dir/stderr.txt"
  status=$?
fi
printf '%s\n' "$status" > "$out_dir/exit_code.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$out_dir/end_time.txt"
exit "$status"
