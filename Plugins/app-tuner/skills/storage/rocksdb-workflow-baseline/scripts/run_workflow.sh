#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

ORIG_ARGS=("$@")
WORK_ROOT=${WORK_ROOT:-/data/rocksdb-baseline}
RESULT_DIR=${RESULT_DIR:-}
DATA_DIR=${DATA_DIR:-/data/workload_data}
CPU_LIST=${CPU_LIST:-0-15}
NUMA_NODE=${NUMA_NODE:-}
DEPLOY_ONLY=0
TEST_ONLY=0
DRY_RUN=${DRY_RUN:-0}

SKILLS_ROOT=$(cd "$SKILL_ROOT/.." && pwd)
DEPLOY_SKILL=${DEPLOY_SKILL:-$SKILLS_ROOT/rocksdb-deploy-baseline}
TEST_SKILL=${TEST_SKILL:-$SKILLS_ROOT/rocksdb-test-baseline}

usage() {
  cat <<EOF
Usage: $0 [options]
  --deploy-only
  --test-only
  --work-root DIR
  --data-dir DIR
  --result-dir DIR
  --skip-deps
  --rocksdb-src DIR
  --ycsb-src DIR
  --cpu-list LIST
  --numa-node N
  --keep-data
  --force-wipe
  --dry-run
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-only) DEPLOY_ONLY=1; shift ;;
    --test-only) TEST_ONLY=1; shift ;;
    --work-root) WORK_ROOT=$2; shift 2 ;;
    --data-dir) DATA_DIR=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --rocksdb-src) ROCKSDB_SRC=$2; shift 2 ;;
    --ycsb-src) YCSB_SRC=$2; shift 2 ;;
    --cpu-list) CPU_LIST=$2; shift 2 ;;
    --numa-node) NUMA_NODE=$2; shift 2 ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --force-wipe) FORCE_WIPE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

if [[ "$DEPLOY_ONLY" == "1" && "$TEST_ONLY" == "1" ]]; then
  die "--deploy-only and --test-only are mutually exclusive" 2
fi
if [[ -z "$RESULT_DIR" ]]; then
  RESULT_DIR="$WORK_ROOT/workflow/$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$RESULT_DIR/env"
{
  printf '%q ' "$0" "${ORIG_ARGS[@]}"
  printf '\n'
} > "$RESULT_DIR/env/workflow_command.txt"

deploy_entry="$DEPLOY_SKILL/scripts/run_deploy.sh"
test_entry="$TEST_SKILL/scripts/run_test.sh"
required_entries=()
if [[ "$TEST_ONLY" != "1" ]]; then required_entries+=("$deploy_entry"); fi
if [[ "$DEPLOY_ONLY" != "1" ]]; then required_entries+=("$test_entry"); fi
for entry in "${required_entries[@]}"; do
  if [[ ! -r "$entry" ]]; then
    printf 'workflow=failed\nFAILED_PHASE=precheck\nEXIT_CODE=2\nREASON=entry_not_readable:%s\n' "$entry" \
      > "$RESULT_DIR/env/workflow.env"
    die "required workflow entry is not readable: $entry" 2
  fi
done
if [[ "$TEST_ONLY" == "1" && ! -f "$RESULT_DIR/env/deploy.env" ]]; then
  printf 'workflow=failed\nFAILED_PHASE=precheck\nEXIT_CODE=2\nREASON=deploy_env_missing\n' \
    > "$RESULT_DIR/env/workflow.env"
  die "deploy.env not found; run deploy first or pass a shared --result-dir" 2
fi

deploy_args=(--work-root "$WORK_ROOT" --result-dir "$RESULT_DIR")
[[ "${SKIP_DEPS:-0}" == "1" ]] && deploy_args+=(--skip-deps)
[[ -n "${ROCKSDB_SRC:-}" ]] && deploy_args+=(--rocksdb-src "$ROCKSDB_SRC")
[[ -n "${YCSB_SRC:-}" ]] && deploy_args+=(--ycsb-src "$YCSB_SRC")
[[ "$DRY_RUN" == "1" ]] && deploy_args+=(--dry-run)

test_args=(--deploy-env "$RESULT_DIR/env/deploy.env" --work-root "$WORK_ROOT" \
  --data-dir "$DATA_DIR" --result-dir "$RESULT_DIR" --cpu-list "$CPU_LIST")
[[ -n "${NUMA_NODE:-}" ]] && test_args+=(--numa-node "$NUMA_NODE")
[[ "${KEEP_DATA:-0}" == "1" ]] && test_args+=(--keep-data)
[[ "${FORCE_WIPE:-0}" == "1" ]] && test_args+=(--force-wipe)
[[ "$DRY_RUN" == "1" ]] && test_args+=(--dry-run)

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ "$TEST_ONLY" != "1" ]]; then
    bash "$DEPLOY_SKILL/scripts/run_deploy.sh" "${deploy_args[@]}"
  fi
  if [[ "$DEPLOY_ONLY" != "1" ]]; then
    printf 'bash %s/scripts/run_test.sh' "$TEST_SKILL"
    printf ' %q' "${test_args[@]}"
    printf '\n'
  fi
  exit 0
fi

if [[ "$TEST_ONLY" != "1" ]]; then
  if bash "$deploy_entry" "${deploy_args[@]}"; then :; else
    rc=$?
    printf 'workflow=failed\nFAILED_PHASE=deploy\nEXIT_CODE=%s\n' "$rc" > "$RESULT_DIR/env/workflow.env"
    exit "$rc"
  fi
fi

if [[ "$DEPLOY_ONLY" == "1" ]]; then
  printf 'workflow=deploy_only\n' > "$RESULT_DIR/env/workflow.env"
  exit 0
fi

if [[ ! -f "$RESULT_DIR/env/deploy.env" ]]; then
  printf 'workflow=failed\nFAILED_PHASE=precheck\nEXIT_CODE=2\nREASON=deploy_env_missing\n' \
    > "$RESULT_DIR/env/workflow.env"
  die "deploy.env not found; run deploy first or pass a shared --result-dir" 2
fi
if bash "$test_entry" "${test_args[@]}"; then :; else
  rc=$?
  printf 'workflow=failed\nFAILED_PHASE=test\nEXIT_CODE=%s\n' "$rc" > "$RESULT_DIR/env/workflow.env"
  exit "$rc"
fi
printf 'workflow=ok\n' > "$RESULT_DIR/env/workflow.env"
exit 0
