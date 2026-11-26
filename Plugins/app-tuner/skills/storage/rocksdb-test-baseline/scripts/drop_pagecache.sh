#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
NUMA_NODE=${NUMA_NODE:-0}
MEM_MIN_GB=${MEM_MIN_GB:-64}
DROP_CACHES_FILE=${DROP_CACHES_FILE:-/proc/sys/vm/drop_caches}

mkdir -p "$RESULT_DIR/env"

sync
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log_info "dry-run: would write 3 to $DROP_CACHES_FILE"
else
  printf '3\n' > "$DROP_CACHES_FILE"
fi

if [[ "${SKIP_MEMORY_CHECK:-0}" != "1" ]]; then
  check_memory "$NUMA_NODE" "$MEM_MIN_GB" || exit $?
fi

NUMA_FREE_MB=$(numa_free_mb "$NUMA_NODE" 2>/dev/null || true)
{
  printf 'DROP_CACHES_AT=%s\n' "$(date -Is)"
  printf 'NUMA_NODE=%s\n' "$NUMA_NODE"
  printf 'NUMA_FREE_MB=%s\n' "${NUMA_FREE_MB:-unknown}"
  printf 'MEM_MIN_GB=%s\n' "$MEM_MIN_GB"
} > "$RESULT_DIR/env/post_drop_memory.env"

log_info "pagecache dropped; NUMA node $NUMA_NODE free=${NUMA_FREE_MB:-unknown}MB"
