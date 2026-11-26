#!/usr/bin/env bash
# Shared helpers for rocksdb-test-baseline.

SKILL_ROOT="${SKILL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

log_info() { printf '[INFO] %s\n' "$*" >&2; }
log_warn() { printf '[WARN] %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }

die() {
  local message=$1
  local code=${2:-1}
  log_error "$message"
  exit "$code"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1" 2
}

expand_cpu_list() {
  local list=${1:-}
  local -a parts out=()
  local part start end i
  [[ -n "$list" ]] || return 1
  IFS=',' read -r -a parts <<< "$list"
  for part in "${parts[@]}"; do
    [[ "$part" =~ ^[0-9]+(-[0-9]+)?$ ]] || return 1
    if [[ "$part" == *-* ]]; then
      start=${part%-*}
      end=${part#*-}
      (( start <= end )) || return 1
      for ((i = start; i <= end; i++)); do out+=("$i"); done
    else
      out+=("$part")
    fi
  done
  printf '%s\n' "${out[*]}"
}

numa_free_mb() {
  local node=${1:-}
  local value
  [[ -n "$node" ]] || return 1
  value=$(numactl -H 2>/dev/null | awk -v n="$node" '$1 == "node" && $2 == n && $3 == "free:" {print $4; exit}')
  [[ -n "$value" ]] || return 1
  printf '%s\n' "$value"
}

numa_node_for_cpus() {
  local list=${1:-}
  local cpus nodes node node_cpus cpu all
  local -a matches=()
  cpus=$(expand_cpu_list "$list") || return 1
  mapfile -t nodes < <(numactl -H 2>/dev/null | awk '$1 == "node" && $3 == "cpus:" {print $2}')
  [[ ${#nodes[@]} -gt 0 ]] || return 1
  for node in "${nodes[@]}"; do
    node_cpus=$(numactl -H 2>/dev/null | awk -v n="$node" '$1 == "node" && $2 == n && $3 == "cpus:" {for (i = 4; i <= NF; i++) print $i}')
    all=1
    for cpu in $cpus; do
      if ! printf '%s\n' "$node_cpus" | grep -qx "$cpu"; then
        all=0
        break
      fi
    done
    [[ "$all" -eq 1 ]] && matches+=("$node")
  done
  [[ ${#matches[@]} -eq 1 ]] || return 1
  printf '%s\n' "${matches[0]}"
}

check_memory() {
  local node=${1:-}
  local min_gb=${2:-}
  local free_mb min_mb
  [[ -n "$node" && -n "$min_gb" ]] || return 1
  free_mb=$(numa_free_mb "$node") || return 1
  min_mb=$((min_gb * 1024))
  if (( free_mb < min_mb )); then
    log_error "NUMA node $node free ${free_mb}MB < required ${min_mb}MB; stop and ask a human to free memory or change the node"
    return 3
  fi
  return 0
}

validate_data_dir() {
  local path=${1:-}
  local resolved
  [[ -n "$path" ]] || return 1
  [[ "$path" == /* ]] || return 1
  resolved=$(realpath -m -- "$path" 2>/dev/null) || return 1
  [[ "$resolved" != "/" && "$resolved" != "/data" ]] || return 1
  if [[ -e "$resolved" && ! -d "$resolved" ]]; then
    log_error "refusing to take over existing non-directory path: $resolved"
    return 1
  fi
  if [[ -d "$resolved" && -n "$(ls -A "$resolved" 2>/dev/null)" ]]; then
    if [[ ! -e "$resolved/CURRENT" ]] && ! ls "$resolved"/MANIFEST-* >/dev/null 2>&1; then
      log_error "refusing to take over existing non-RocksDB directory: $resolved"
      return 1
    fi
  fi
  return 0
}
