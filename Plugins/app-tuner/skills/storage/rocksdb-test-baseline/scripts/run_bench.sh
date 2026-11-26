#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
DATA_DIR=${DATA_DIR:-/data/workload_data}
YCSB_BIN=${YCSB_BIN:-/data/rocksdb-baseline/artifacts/YCSB_DIST/bin/ycsb}
YCSB_WORKLOADS=${YCSB_WORKLOADS:-/data/rocksdb-baseline/artifacts/YCSB_DIST/workloads}
YCSB_OPTIONS=${YCSB_OPTIONS:-}
CPU_LIST=${CPU_LIST:-0-15}
NUMA_NODE=${NUMA_NODE:-0}
BENCH_THREADS=${BENCH_THREADS:-5}
WORKLOADS=${WORKLOADS:-a b c d e f}
RECORD_COUNT=${RECORD_COUNT:-1000000}
OPERATION_COUNT=${OPERATION_COUNT:-1000000}

mkdir -p "$RESULT_DIR/env"
[[ -x "$YCSB_BIN" ]] || die "YCSB executable not found: $YCSB_BIN" 2
[[ -f "$YCSB_OPTIONS" ]] || die "options file not found: $YCSB_OPTIONS" 2
[[ -d "$DATA_DIR" ]] || die "data directory not found: $DATA_DIR" 2

run_ts=$(date +%Y%m%d%H%M%S)
done_list=""
status=ok
failed=""

for w in $WORKLOADS; do
  [[ -f "$YCSB_WORKLOADS/workload$w" ]] || die "missing workload$w under $YCSB_WORKLOADS" 2
  log_name="baseline_workload${w}_${run_ts}.log"
  log_info "benchmark workload$w: ops=$OPERATION_COUNT threads=$BENCH_THREADS"
  if ! numactl --physcpubind="$CPU_LIST" --membind="$NUMA_NODE" \
    "$YCSB_BIN" run rocksdb -s \
    -P "$YCSB_WORKLOADS/workload$w" \
    -p "recordcount=$RECORD_COUNT" \
    -p "operationcount=$OPERATION_COUNT" \
    -p "rocksdb.dir=$DATA_DIR" \
    -p "rocksdb.optionsfile=$YCSB_OPTIONS" \
    -p table=default \
    -threads "$BENCH_THREADS" > "$RESULT_DIR/$log_name" 2>&1; then
    status=failed
    failed=$w
    break
  fi
  done_list="$done_list${done_list:+ }$w"
done

{
  printf 'WORKLOADS_DONE=%s\n' "$done_list"
  printf 'FAILED_WORKLOAD=%s\n' "$failed"
  printf 'BENCH_STATUS=%s\n' "$status"
  printf 'BENCH_THREADS=%s\n' "$BENCH_THREADS"
} > "$RESULT_DIR/env/bench.env"

if [[ "$status" != "ok" ]]; then
  log_error "benchmark failed at workload$failed"
  exit 1
fi

log_info "benchmark OK: $done_list"
