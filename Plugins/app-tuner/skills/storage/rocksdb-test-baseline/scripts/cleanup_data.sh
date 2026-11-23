#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

DATA_DIR=/data/workload_data
DRY_RUN_FLAG=0
KEEP_DATA=0

usage() {
  printf 'Usage: %s [--data-dir DIR] [--keep-data] [--dry-run]\n' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR=$2; shift 2 ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --dry-run) DRY_RUN_FLAG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" 2 ;;
  esac
done

validate_data_dir "$DATA_DIR" || die "unsafe data dir: $DATA_DIR" 2

resolved_data=$(realpath -m -- "$DATA_DIR" 2>/dev/null) || die "cannot resolve data dir: $DATA_DIR" 2
if command -v findmnt >/dev/null 2>&1; then
  target=$(findmnt -n -T "$resolved_data" -o TARGET 2>/dev/null || true)
  [[ "$target" != "$resolved_data" ]] || die "refusing to move mount point: $resolved_data" 2
fi

data_bytes=0
if [[ -e "$DATA_DIR" ]]; then
  data_bytes=$(du -sb "$DATA_DIR" 2>/dev/null | awk '{print $1}')
fi

status=no-data
backup_dir=""

if [[ "$KEEP_DATA" -eq 1 ]]; then
  status=skipped
  log_info "data kept at $DATA_DIR"
elif [[ ! -e "$DATA_DIR" ]]; then
  status=no-data
  log_info "no data directory to back up: $DATA_DIR"
elif [[ "$DRY_RUN_FLAG" -eq 1 ]]; then
  status=dry-run
  backup_dir="${DATA_DIR}.backup.$(date +%Y%m%d-%H%M%S)-$$"
  log_info "dry-run: would rename $DATA_DIR to $backup_dir"
else
  backup_dir="${DATA_DIR}.backup.$(date +%Y%m%d-%H%M%S)-$$"
  mv "$DATA_DIR" "$backup_dir"
  status=backed_up
  log_info "data backed up to $backup_dir; remove it manually after review"
fi

if [[ -n "${RESULT_DIR:-}" ]]; then
  mkdir -p "$RESULT_DIR/env"
  {
    printf 'DATA_BYTES=%s\n' "$data_bytes"
    printf 'BACKUP_DIR=%s\n' "$backup_dir"
    printf 'BACKUP_STATUS=%s\n' "$status"
    printf 'FREED_BYTES=0\n'
  } > "$RESULT_DIR/env/cleanup.env"
fi

if [[ "$status" == "backed_up" ]]; then
  log_info "backup complete; data is not deleted automatically"
fi
