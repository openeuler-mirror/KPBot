#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
DATA_DIR=${DATA_DIR:-/data/workload_data}
YCSB_BIN=${YCSB_BIN:-/data/rocksdb-baseline/artifacts/YCSB_DIST/bin/ycsb}
YCSB_WORKLOADS=${YCSB_WORKLOADS:-/data/rocksdb-baseline/artifacts/YCSB_DIST/workloads}
YCSB_OPTIONS=${YCSB_OPTIONS:-}
RECORD_COUNT=${RECORD_COUNT:-1000000}
OPERATION_COUNT=${OPERATION_COUNT:-1000000}
LOAD_THREADS=${LOAD_THREADS:-5}

mkdir -p "$RESULT_DIR/env"
validate_data_dir "$DATA_DIR" || die "unsafe DATA_DIR: $DATA_DIR" 2
[[ -x "$YCSB_BIN" ]] || die "YCSB executable not found: $YCSB_BIN" 2
[[ -f "$YCSB_WORKLOADS/workloada" ]] || die "YCSB workloada not found under $YCSB_WORKLOADS" 2
[[ -f "$YCSB_OPTIONS" ]] || die "options file not found: $YCSB_OPTIONS" 2

pre_load_backup=""
if [[ -e "$DATA_DIR" ]]; then
  resolved_data=$(realpath -m -- "$DATA_DIR" 2>/dev/null) || die "cannot resolve DATA_DIR: $DATA_DIR" 2
  if command -v findmnt >/dev/null 2>&1; then
    target=$(findmnt -n -T "$resolved_data" -o TARGET 2>/dev/null || true)
    [[ "$target" != "$resolved_data" ]] || die "refusing to move mount point: $resolved_data" 2
  fi
  pre_load_backup="${DATA_DIR}.backup.$(date +%Y%m%d-%H%M%S)-$$"
  mv "$DATA_DIR" "$pre_load_backup"
  log_info "existing data backed up to $pre_load_backup; remove it manually after review"
fi
mkdir -p "$DATA_DIR"

log_info "load: records=$RECORD_COUNT threads=$LOAD_THREADS dir=$DATA_DIR"
load_rc=0
"$YCSB_BIN" load rocksdb -s \
  -P "$YCSB_WORKLOADS/workloada" \
  -p "recordcount=$RECORD_COUNT" \
  -p "operationcount=$OPERATION_COUNT" \
  -p "rocksdb.dir=$DATA_DIR" \
  -p "rocksdb.optionsfile=$YCSB_OPTIONS" \
  -p table=default \
  -threads "$LOAD_THREADS" > "$RESULT_DIR/load.log" 2>&1 || load_rc=$?

{
  printf 'LOAD_RC=%s\n' "$load_rc"
  printf 'RECORD_COUNT=%s\n' "$RECORD_COUNT"
  printf 'OPERATION_COUNT=%s\n' "$OPERATION_COUNT"
  printf 'FIELD_LENGTH=default\n'
  printf 'FIELD_COUNT=default\n'
  printf 'LOAD_THREADS=%s\n' "$LOAD_THREADS"
  printf 'PRE_LOAD_BACKUP=%s\n' "$pre_load_backup"
  printf 'LOAD_LOG=%s\n' "$RESULT_DIR/load.log"
} > "$RESULT_DIR/env/load.env"

if [[ "$load_rc" -ne 0 ]]; then
  log_error "load failed with exit code $load_rc; pre-load data backup at ${pre_load_backup:-none}"
  exit "$load_rc"
fi

log_info "load OK: $RESULT_DIR/load.log"
