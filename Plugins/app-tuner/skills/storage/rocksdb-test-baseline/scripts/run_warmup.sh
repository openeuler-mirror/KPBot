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
WARMUP_THREADS=${WARMUP_THREADS:-3}
WARMUP_OPERATION_COUNT=${WARMUP_OPERATION_COUNT:-1000000}
WARMUP_MAX_ROUNDS=${WARMUP_MAX_ROUNDS:-10}
RECORD_COUNT=${RECORD_COUNT:-1000000}

mkdir -p "$RESULT_DIR/env"
[[ -x "$YCSB_BIN" ]] || die "YCSB executable not found: $YCSB_BIN" 2
[[ -f "$YCSB_WORKLOADS/workloadc" ]] || die "YCSB workloadc not found under $YCSB_WORKLOADS" 2
[[ -f "$YCSB_OPTIONS" ]] || die "options file not found: $YCSB_OPTIONS" 2
[[ -d "$DATA_DIR" ]] || die "data directory not found: $DATA_DIR" 2

LOG="$DATA_DIR/LOG"
round=1
started=0
finished=0
status=ok

while true; do
  log_info "warmup round $round: ops=$WARMUP_OPERATION_COUNT threads=$WARMUP_THREADS"
  numactl --physcpubind="$CPU_LIST" --membind="$NUMA_NODE" \
    "$YCSB_BIN" run rocksdb -s \
    -P "$YCSB_WORKLOADS/workloadc" \
    -p "recordcount=$RECORD_COUNT" \
    -p "operationcount=$WARMUP_OPERATION_COUNT" \
    -p "rocksdb.dir=$DATA_DIR" \
    -p "rocksdb.optionsfile=$YCSB_OPTIONS" \
    -p table=default \
    -threads "$WARMUP_THREADS" > "$RESULT_DIR/warmup_round_$round.log" 2>&1

  if [[ ! -f "$LOG" ]]; then
    log_error "current RocksDB LOG missing after warmup round $round: $LOG"
    exit 5
  fi
  started=$(grep -c 'compaction_started' "$LOG" 2>/dev/null || true)
  finished=$(grep -c 'compaction_finished' "$LOG" 2>/dev/null || true)
  printf 'round=%s started=%s finished=%s\n' "$round" "$started" "$finished" \
    >> "$RESULT_DIR/warmup_rounds.txt"

  if [[ "$started" -eq 0 ]]; then
    break
  fi
  if [[ "$round" -ge "$WARMUP_MAX_ROUNDS" ]]; then
    status=max_rounds_exceeded
    break
  fi
  round=$((round + 1))
done

{
  printf 'ROUNDS=%s\n' "$round"
  printf 'FINAL_STARTED=%s\n' "$started"
  printf 'FINAL_FINISHED=%s\n' "$finished"
  printf 'WARMUP_OPERATION_COUNT=%s\n' "$WARMUP_OPERATION_COUNT"
  printf 'WARMUP_STATUS=%s\n' "$status"
} > "$RESULT_DIR/env/warmup.env"

if [[ "$status" != "ok" ]]; then
  log_error "warmup did not finish within $WARMUP_MAX_ROUNDS rounds; current LOG still has compaction_started"
  exit 4
fi

log_info "warmup OK: rounds=$round final_started=$started final_finished=$finished"
