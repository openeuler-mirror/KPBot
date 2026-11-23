#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
RESULT_DIR=${RESULT_DIR:-}
SKIP_DEPS=${SKIP_DEPS:-0}
DRY_RUN=${DRY_RUN:-0}
ROCKSDB_SRC=${ROCKSDB_SRC:-}
YCSB_SRC=${YCSB_SRC:-}
MAVEN_SETTINGS=${MAVEN_SETTINGS:-}

usage() {
  cat <<EOF
Usage: $0 [options]
  --work-root DIR       default $WORK_ROOT
  --result-dir DIR      default \$WORK_ROOT/deploy/<timestamp>
  --skip-deps            skip dependency installation
  --rocksdb-src DIR      reuse a RocksDB source tree
  --ycsb-src DIR         clone YCSB from this local source
  --maven-settings FILE  pass -s FILE to Maven
  --dry-run              print commands without executing
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-root) WORK_ROOT=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --rocksdb-src) ROCKSDB_SRC=$2; shift 2 ;;
    --ycsb-src) YCSB_SRC=$2; shift 2 ;;
    --maven-settings) MAVEN_SETTINGS=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

if [[ -z "$RESULT_DIR" ]]; then
  RESULT_DIR="$WORK_ROOT/deploy/$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$RESULT_DIR/env" "$WORK_ROOT"

run_id="$(date +%Y%m%d-%H%M%S)-$$"
state="$RESULT_DIR/env/deploy_state_${run_id}.env"
logs_dir="$RESULT_DIR/logs"
mkdir -p "$logs_dir"

export WORK_ROOT RESULT_DIR SKIP_DEPS DRY_RUN ROCKSDB_SRC YCSB_SRC MAVEN_SETTINGS
export YCSB_COMMIT=${YCSB_COMMIT:-19e885f7cb780fdded0547853f7810a150554caf}
export ROCKSDB_VERSION=${ROCKSDB_VERSION:-6.26.1}
export MAVEN_REPO=${MAVEN_REPO:-$WORK_ROOT/m2/repository}
export YCSB_RUN_ID=$run_id

if [[ "$SKIP_DEPS" == "1" ]]; then
  for cmd in git make gcc g++ java javac mvn tar; do require_cmd "$cmd"; done
  if [[ -n "$MAVEN_SETTINGS" && ! -f "$MAVEN_SETTINGS" ]]; then
    die "maven settings file not found: $MAVEN_SETTINGS" 2
  fi
  mkdir -p "$MAVEN_REPO"
fi

run_phase() {
  local name=$1
  local script=$2
  local log="$logs_dir/$name.log"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'bash %s/scripts/%s\n' "$SKILL_ROOT" "$script"
    return 0
  fi
  log_info "deploy phase: $name (log: $log)"
  local rc=0
  bash "$SKILL_ROOT/scripts/$script" >"$log" 2>&1 || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf 'phase=%s status=failed exit_code=%s log=%s\n' "$name" "$rc" "$log" >> "$state"
    printf 'DEPLOY_STATUS=failed\nFAILED_PHASE=%s\nFAILED_EXIT_CODE=%s\nFAILED_LOG=%s\n' \
      "$name" "$rc" "$log" >> "$RESULT_DIR/env/deploy.env"
    log_error "deploy phase failed: $name; see $log"
    return "$rc"
  fi
  printf 'phase=%s status=ok log=%s\n' "$name" "$log" >> "$state"
  return 0
}

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ "$SKIP_DEPS" != "1" ]]; then
    printf 'bash %s/scripts/install_deps.sh\n' "$SKILL_ROOT"
  fi
  printf 'bash %s/scripts/build_rocksdb.sh\n' "$SKILL_ROOT"
  printf 'bash %s/scripts/build_ycsb.sh\n' "$SKILL_ROOT"
  exit 0
fi

if [[ -f "$RESULT_DIR/env/deploy.env" ]]; then
  mv "$RESULT_DIR/env/deploy.env" "$RESULT_DIR/env/deploy.env.previous.${run_id}"
  log_info "previous deploy.env backed up to deploy.env.previous.${run_id}"
fi
printf 'DEPLOY_STATUS=running\nDEPLOY_RUN_ID=%s\n' "$run_id" > "$RESULT_DIR/env/deploy.env"
printf 'run_id=%s\n' "$run_id" > "$state"

if [[ "$SKIP_DEPS" != "1" ]]; then
  if run_phase deps install_deps.sh; then :; else exit $?; fi
fi
if run_phase build_rocksdb build_rocksdb.sh; then :; else exit $?; fi
if run_phase build_ycsb build_ycsb.sh; then :; else exit $?; fi

[[ -f "$RESULT_DIR/env/deploy.env" ]] || die "deploy.env was not generated" 2
printf 'DEPLOY_OK=%s\n' "$(date -Is)" >> "$RESULT_DIR/env/deploy.env"
printf 'DEPLOY_STATUS=ok\n' >> "$RESULT_DIR/env/deploy.env"
log_info "deploy complete: $RESULT_DIR/env/deploy.env"
