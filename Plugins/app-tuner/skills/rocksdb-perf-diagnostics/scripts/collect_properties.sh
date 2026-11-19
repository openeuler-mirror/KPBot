#!/usr/bin/env bash
# Collect key RocksDB properties with ldb (read-only, no code change).
#
# Usage:
#   ./collect_properties.sh /path/to/db [--full]
#   LDB=/path/to/ldb ./collect_properties.sh /path/to/db
#
# Note: ldb opens the DB and may fail if another process holds the lock.
# For a live service, prefer reading properties through the application
# (GetProperty / getProperty) or a read-only replica/checkpoint.

set -euo pipefail

DB="${1:-}"
if [[ -z "$DB" ]]; then
  echo "usage: $0 /path/to/db [--full]" >&2
  exit 2
fi
FULL="${2:-}"
LDB="${LDB:-ldb}"

if ! command -v "$LDB" >/dev/null 2>&1; then
  echo "error: '$LDB' not found; build it from the rocksdb repo (make ldb) or set LDB=..." >&2
  exit 1
fi

PROPS=(
  rocksdb.num-files-at-level0
  rocksdb.num-files-at-level1
  rocksdb.num-files-at-level2
  rocksdb.num-files-at-level3
  rocksdb.num-files-at-level4
  rocksdb.num-files-at-level5
  rocksdb.num-files-at-level6
  rocksdb.is-write-stopped
  rocksdb.actual-delayed-write-rate
  rocksdb.estimate-pending-compaction-bytes
  rocksdb.compaction-pending
  rocksdb.mem-table-flush-pending
  rocksdb.num-immutable-mem-table
  rocksdb.num-running-compactions
  rocksdb.num-running-flushes
  rocksdb.cur-size-all-mem-tables
  rocksdb.estimate-live-data-size
  rocksdb.live-sst-files-size
  rocksdb.total-sst-files-size
  rocksdb.block-cache-usage
  rocksdb.block-cache-pinned-usage
  rocksdb.background-errors
  rocksdb.num-live-versions
  rocksdb.num-snapshots
  rocksdb.oldest-snapshot-sequence
  rocksdb.estimate-num-keys
)

printf '%-46s %s\n' "PROPERTY" "VALUE"
printf '%-46s %s\n' "--------" "-----"
for p in "${PROPS[@]}"; do
  out="$("$LDB" --db="$DB" get_property "$p" 2>/dev/null || true)"
  printf '%-46s %s\n' "$p" "${out:-<unavailable>}"
done

echo
echo "=== rocksdb.levelstats ==="
"$LDB" --db="$DB" get_property rocksdb.levelstats 2>/dev/null || echo "<unavailable>"

if [[ "$FULL" == "--full" ]]; then
  echo
  echo "=== rocksdb.stats ==="
  "$LDB" --db="$DB" get_property rocksdb.stats 2>/dev/null || echo "<unavailable>"
fi
