#!/usr/bin/env bash
# Deterministic mock of db_bench for the "execute optimization" eval.
# It is NOT a real benchmark: metrics are a pure function of the arguments,
# so the eval can check the optimization loop (baseline -> one change -> rerun).
#
# Every invocation is appended to $MOCK_LOG so the grader can verify that
# only one variable changed between baseline and the follow-up run.
#
# Usage:
#   ./mock_db_bench.sh --write_buffer_size=64MB --max_background_jobs=2 --out=/tmp/x.json
#   MOCK_LOG=/tmp/inv.log ./mock_db_bench.sh ...

set -euo pipefail

MOCK_LOG="${MOCK_LOG:-/tmp/opencode/mock_db_bench_invocations.log}"
mkdir -p "$(dirname "$MOCK_LOG")"
printf '%s\n' "$*" >> "$MOCK_LOG"

wbs_mb=64
jobs=2
out=""

for arg in "$@"; do
  case "$arg" in
    --write_buffer_size=*)
      v="${arg#*=}"
      v="${v%MB}"; v="${v%mb}"
      [[ "$v" =~ ^[0-9]+$ ]] && wbs_mb="$v"
      ;;
    --max_background_jobs=*)
      v="${arg#*=}"
      [[ "$v" =~ ^[0-9]+$ ]] && jobs="$v"
      ;;
    --out=*)
      out="${arg#*=}"
      ;;
    --statistics=*|--histogram=*|--perf_level=*|--level0_file_num_compaction_trigger=*|--compression=*)
      : # accepted for realism, no effect in the mock
      ;;
  esac
done

# Deterministic model: more background jobs / a larger memtable reduce stalls.
stall=$(( 120 - (jobs - 2) * 20 - (wbs_mb / 64) * 10 ))
(( stall < 0 )) && stall=0

p99=$(python3 -c "print(round(24.0 - ($jobs - 2) * 1.5 - (($wbs_mb - 64) / 64) * 0.8, 2))")
throughput=$(python3 -c "print(int(10000 + ($jobs - 2) * 600 + (($wbs_mb - 64) / 64) * 900))")

json=$(printf '{"write_buffer_size_mb":%d,"max_background_jobs":%d,"stall_count":%d,"p99_ms":%.2f,"throughput_ops":%d}\n' \
  "$wbs_mb" "$jobs" "$stall" "$p99" "$throughput")

if [[ -n "$out" ]]; then
  printf '%s\n' "$json" > "$out"
fi
printf '%s\n' "$json"
