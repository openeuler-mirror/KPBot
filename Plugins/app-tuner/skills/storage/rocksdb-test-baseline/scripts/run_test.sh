#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

ORIG_ARGS=("$@")
WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
DATA_DIR=${DATA_DIR:-/data/workload_data}
RESULT_DIR=${RESULT_DIR:-}
DEPLOY_ENV=${DEPLOY_ENV:-}
CPU_LIST=${CPU_LIST:-0-15}
NUMA_NODE=${NUMA_NODE:-}
MEM_MIN_GB=${MEM_MIN_GB:-64}
DISK_MIN_GB=${DISK_MIN_GB:-50}
RECORD_COUNT=${RECORD_COUNT:-1000000}
OPERATION_COUNT=${OPERATION_COUNT:-1000000}
LOAD_THREADS=${LOAD_THREADS:-5}
WARMUP_THREADS=${WARMUP_THREADS:-3}
WARMUP_OPERATION_COUNT=${WARMUP_OPERATION_COUNT:-1000000}
WARMUP_MAX_ROUNDS=${WARMUP_MAX_ROUNDS:-10}
BENCH_THREADS=${BENCH_THREADS:-5}
WORKLOADS=${WORKLOADS:-"a b c d e f"}
KEEP_DATA=${KEEP_DATA:-0}
FORCE_WIPE=${FORCE_WIPE:-0}
DRY_RUN=${DRY_RUN:-0}
PROFILE=${PROFILE:-baseline}

usage() {
  cat <<EOF
Usage: $0 [options]
  --deploy-env FILE      deploy.env from rocksdb-deploy-baseline
  --work-root DIR        default $WORK_ROOT
  --data-dir DIR         default $DATA_DIR
  --result-dir DIR       required for a shared run
  --cpu-list LIST        default $CPU_LIST
  --numa-node N          default from preflight
  --keep-data
  --force-wipe
  --dry-run
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-env) DEPLOY_ENV=$2; shift 2 ;;
    --work-root) WORK_ROOT=$2; shift 2 ;;
    --data-dir) DATA_DIR=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; shift 2 ;;
    --cpu-list) CPU_LIST=$2; shift 2 ;;
    --numa-node) NUMA_NODE=$2; shift 2 ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --force-wipe) FORCE_WIPE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

if [[ -z "$RESULT_DIR" ]]; then
  RESULT_DIR="$WORK_ROOT/test/$(date +%Y%m%d-%H%M%S)"
fi
if [[ -z "$DEPLOY_ENV" ]]; then
  DEPLOY_ENV="$RESULT_DIR/env/deploy.env"
fi
[[ -f "$DEPLOY_ENV" ]] || die "deploy.env not found: $DEPLOY_ENV" 2
mkdir -p "$RESULT_DIR/env"

for key in YCSB_BIN YCSB_WORKLOADS YCSB_OPTIONS; do
  value=$(awk -F= -v k="$key" '$1 == k {print $2; exit}' "$DEPLOY_ENV")
  [[ -n "$value" ]] || die "deploy.env missing $key" 2
  export "$key=$value"
done

baseline_exact=1
[[ "$RECORD_COUNT" == "1000000" ]] || baseline_exact=0
[[ "$OPERATION_COUNT" == "1000000" ]] || baseline_exact=0
[[ "$LOAD_THREADS" == "5" ]] || baseline_exact=0
[[ "$WARMUP_THREADS" == "3" ]] || baseline_exact=0
[[ "$BENCH_THREADS" == "5" ]] || baseline_exact=0
[[ "$WARMUP_OPERATION_COUNT" == "1000000" ]] || baseline_exact=0
[[ "$WORKLOADS" == "a b c d e f" ]] || baseline_exact=0
[[ "$CPU_LIST" == "0-15" ]] || baseline_exact=0
if [[ "$baseline_exact" != "1" && "$PROFILE" == "baseline" ]]; then
  PROFILE=custom
fi
if [[ "$DRY_RUN" == "1" ]]; then
  printf 'profile=%s\n' "$PROFILE"
fi

validate_data_dir "$DATA_DIR" || die "unsafe --data-dir: $DATA_DIR" 2
if [[ "$DRY_RUN" != "1" && "$DATA_DIR" != "/data/workload_data" && "$FORCE_WIPE" != "1" ]]; then
  die "custom --data-dir requires --force-wipe: $DATA_DIR" 2
fi

export WORK_ROOT DATA_DIR RESULT_DIR DEPLOY_ENV CPU_LIST NUMA_NODE
export MEM_MIN_GB DISK_MIN_GB PROFILE
export RECORD_COUNT OPERATION_COUNT
export LOAD_THREADS WARMUP_THREADS BENCH_THREADS WARMUP_OPERATION_COUNT
export WARMUP_MAX_ROUNDS WORKLOADS KEEP_DATA FORCE_WIPE DRY_RUN

run_id="$(date +%Y%m%d-%H%M%S)-$$"

write_cleanup_not_started() {
  mkdir -p "$RESULT_DIR/env"
  printf 'BACKUP_STATUS=not_started\nDATA_BYTES=0\nBACKUP_DIR=\n' > "$RESULT_DIR/env/cleanup.env"
}

backup_previous_test_artifacts() {
  local previous="$RESULT_DIR/previous.$run_id"
  local f
  mkdir -p "$previous/env"
  for f in "$RESULT_DIR"/load.log "$RESULT_DIR"/warmup_round_*.log "$RESULT_DIR"/warmup_rounds.txt \
           "$RESULT_DIR"/baseline_workload*.log "$RESULT_DIR"/summary.json "$RESULT_DIR"/summary.csv \
           "$RESULT_DIR"/REPORT.md; do
    [[ -e "$f" ]] || continue
    mv "$f" "$previous/$(basename "$f")"
  done
  for f in "$RESULT_DIR"/env/load.env "$RESULT_DIR"/env/warmup.env "$RESULT_DIR"/env/bench.env \
           "$RESULT_DIR"/env/post_drop_memory.env "$RESULT_DIR"/env/cleanup.env \
           "$RESULT_DIR"/env/test_state.env "$RESULT_DIR"/env/command.txt \
           "$RESULT_DIR"/env/collected.json "$RESULT_DIR"/env/rocksdb.env \
           "$RESULT_DIR"/env/ycsb.env; do
    [[ -e "$f" ]] || continue
    mv "$f" "$previous/env/$(basename "$f")"
  done
  log_info "previous test artifacts backed up to $previous"
}

run_phase() {
  local name=$1
  local script=$2
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'bash %s/scripts/%s\n' "$SKILL_ROOT" "$script"
    return 0
  fi
  log_info "test phase: $name"
  local rc=0
  bash "$SKILL_ROOT/scripts/$script" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf '%s=failed rc=%s\n' "$name" "$rc" >> "$state"
    return "$rc"
  fi
  printf '%s=ok\n' "$name" >> "$state"
  return 0
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'profile=%s\n' "$PROFILE"
  printf 'bash %s/scripts/preflight.sh\n' "$SKILL_ROOT"
  printf 'bash %s/scripts/run_load.sh\n' "$SKILL_ROOT"
  printf 'bash %s/scripts/drop_pagecache.sh\n' "$SKILL_ROOT"
  printf 'bash %s/scripts/run_warmup.sh\n' "$SKILL_ROOT"
  printf 'bash %s/scripts/run_bench.sh\n' "$SKILL_ROOT"
  printf 'python3 %s/scripts/report.py --result-dir %s --data-dir %s --profile %s\n' \
    "$SKILL_ROOT" "$RESULT_DIR" "$DATA_DIR" "$PROFILE"
  printf 'bash %s/scripts/cleanup_data.sh --data-dir %s\n' "$SKILL_ROOT" "$DATA_DIR"
  exit 0
fi

mkdir -p "$RESULT_DIR/env"
if [[ "$(realpath -m "$DEPLOY_ENV")" != "$(realpath -m "$RESULT_DIR/env/deploy.env")" ]]; then
  cp "$DEPLOY_ENV" "$RESULT_DIR/env/deploy.env"
fi
backup_previous_test_artifacts
printf '%s\n' "$0 ${ORIG_ARGS[*]}" > "$RESULT_DIR/env/command.txt"
state="$RESULT_DIR/env/test_state.env"
: > "$state"

if run_phase preflight preflight.sh; then :; else
  rc=$?
  write_cleanup_not_started
  log_error "preflight failed with exit code $rc; data directory was not touched"
  exit "$rc"
fi
if [[ -f "$RESULT_DIR/env/preflight.env" ]]; then
  node=$(awk -F= '$1 == "NUMA_NODE" {print $2; exit}' "$RESULT_DIR/env/preflight.env" || true)
  if [[ -n "$node" ]]; then NUMA_NODE="$node"; export NUMA_NODE; fi
fi

failed=0
exit_code=1
if run_phase load run_load.sh; then :; else exit_code=$?; failed=1; fi
if [[ "$failed" == "0" ]]; then
  if run_phase drop_cache drop_pagecache.sh; then :; else exit_code=$?; failed=1; fi
fi
if [[ "$failed" == "0" ]]; then
  if run_phase warmup run_warmup.sh; then :; else exit_code=$?; failed=1; fi
fi
if [[ "$failed" == "0" ]]; then
  if run_phase bench run_bench.sh; then :; else exit_code=$?; failed=1; fi
fi

if [[ "$failed" == "0" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'python3 %s/scripts/report.py --result-dir %s --data-dir %s --profile %s\n' \
      "$SKILL_ROOT" "$RESULT_DIR" "$DATA_DIR" "$PROFILE"
  else
    if python3 "$SKILL_ROOT/scripts/report.py" --result-dir "$RESULT_DIR" \
      --data-dir "$DATA_DIR" --profile "$PROFILE"; then
      printf 'report=ok\n' >> "$state"
    else
      exit_code=$?; failed=1
    fi
  fi
fi

if [[ "$failed" == "0" ]]; then
  cleanup_args=(--data-dir "$DATA_DIR")
  [[ "$KEEP_DATA" == "1" ]] && cleanup_args+=(--keep-data)
  if bash "$SKILL_ROOT/scripts/cleanup_data.sh" "${cleanup_args[@]}"; then
    printf 'cleanup=ok\n' >> "$state"
  else
    cleanup_rc=$?
    printf 'BACKUP_STATUS=failed\nBACKUP_EXIT_CODE=%s\nBACKUP_DIR=\n' "$cleanup_rc" \
      > "$RESULT_DIR/env/cleanup.env"
    printf 'cleanup=failed rc=%s\n' "$cleanup_rc" >> "$state"
    exit_code=$cleanup_rc
    failed=1
  fi
  report_rc=0
  python3 "$SKILL_ROOT/scripts/report.py" --result-dir "$RESULT_DIR" \
    --data-dir "$DATA_DIR" --profile "$PROFILE" || report_rc=$?
  if [[ "$report_rc" -ne 0 ]]; then
    [[ "$failed" == "0" ]] && exit_code=$report_rc
    failed=1
  fi
  [[ "$failed" == "0" ]] && exit 0
fi

if [[ "$DRY_RUN" != "1" ]]; then
  if [[ "${KEEP_DATA:-0}" != "1" ]]; then
    prior_backup_status=$(awk -F= '$1 == "BACKUP_STATUS" {print $2; exit}' \
      "$RESULT_DIR/env/cleanup.env" 2>/dev/null || true)
    if [[ "$prior_backup_status" == "backed_up" ]]; then
      printf 'cleanup=skipped-data-already-backed-up\n' >> "$state"
    elif [[ "$prior_backup_status" == "failed" ]]; then
      if bash "$SKILL_ROOT/scripts/cleanup_data.sh" --data-dir "$DATA_DIR"; then
        sed -i 's/^BACKUP_STATUS=.*/BACKUP_STATUS=retry-backed-up-after-failure/' \
          "$RESULT_DIR/env/cleanup.env"
        printf 'BACKUP_EXIT_CODE=%s\n' "${cleanup_rc:-1}" >> "$RESULT_DIR/env/cleanup.env"
        printf 'cleanup=retry-backed-up rc=%s\n' "${cleanup_rc:-1}" >> "$state"
      else
        retry_rc=$?
        printf 'BACKUP_STATUS=failed\nBACKUP_EXIT_CODE=%s\nBACKUP_DIR=\n' "${cleanup_rc:-$retry_rc}" \
          > "$RESULT_DIR/env/cleanup.env"
        printf 'cleanup=retry-failed rc=%s\n' "${cleanup_rc:-$retry_rc}" >> "$state"
      fi
    elif bash "$SKILL_ROOT/scripts/cleanup_data.sh" --data-dir "$DATA_DIR"; then
      printf 'cleanup=failed-run-backup-ok\n' >> "$state"
    else
      cleanup_rc=$?
      printf 'BACKUP_STATUS=failed\nBACKUP_EXIT_CODE=%s\nBACKUP_DIR=\n' "$cleanup_rc" \
        > "$RESULT_DIR/env/cleanup.env"
      printf 'cleanup=failed rc=%s\n' "$cleanup_rc" >> "$state"
    fi
  fi
  python3 "$SKILL_ROOT/scripts/report.py" --result-dir "$RESULT_DIR" \
    --data-dir "$DATA_DIR" --profile "$PROFILE" >/dev/null 2>&1 || true
fi
if [[ "${KEEP_DATA:-0}" == "1" ]]; then
  log_error "baseline test failed; data kept at $DATA_DIR"
else
  log_error "baseline test failed; see env/cleanup.env for the backup path"
fi
exit "$exit_code"
