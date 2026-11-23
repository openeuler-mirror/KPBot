#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)/common.sh"

RESULT_DIR=${RESULT_DIR:-/tmp/rocksdb-baseline-result}
DATA_DIR=${DATA_DIR:-/data/workload_data}
CPU_LIST=${CPU_LIST:-0-15}
NUMA_NODE=${NUMA_NODE:-}
MEM_MIN_GB=${MEM_MIN_GB:-64}
DISK_MIN_GB=${DISK_MIN_GB:-50}

mkdir -p "$RESULT_DIR/env"

if [[ "${SKIP_ROOT_CHECK:-0}" != "1" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "preflight requires root for yum and drop_caches" 2
fi

for cmd in numactl df lsblk free python3; do
  require_cmd "$cmd"
done

ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" ]]; then
  die "preflight requires aarch64, got $ARCH" 2
fi

actual_node=$(numa_node_for_cpus "$CPU_LIST") || die "CPU list $CPU_LIST spans NUMA nodes or is invalid" 2
if [[ -n "$NUMA_NODE" && "$NUMA_NODE" != "$actual_node" ]]; then
  die "CPU list $CPU_LIST belongs to NUMA node $actual_node, not requested node $NUMA_NODE" 2
fi
NUMA_NODE="$actual_node"

check_memory "$NUMA_NODE" "$MEM_MIN_GB" || exit $?

probe_dir=$DATA_DIR
[[ -d "$probe_dir" ]] || probe_dir=$(dirname "$probe_dir")
avail_kb=$(df -Pk "$probe_dir" 2>/dev/null | awk 'NR == 2 {print $4}' || true)
[[ -n "$avail_kb" ]] || die "cannot determine free space for $DATA_DIR" 2
disk_min_kb=$((DISK_MIN_GB * 1024 * 1024))
if (( avail_kb < disk_min_kb )); then
  die "disk free ${avail_kb}KB < required ${disk_min_kb}KB at $DATA_DIR" 2
fi

OS=$(awk -F= '/^PRETTY_NAME=/{gsub(/"/, "", $2); print $2; exit}' /etc/os-release 2>/dev/null || true)
[[ -n "$OS" ]] || OS="unknown"
KERNEL=$(uname -r)
CPU_MODEL=$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^ +/, "", $2); print $2; exit}' || true)
[[ -n "$CPU_MODEL" ]] || CPU_MODEL="unknown"
NUMA_FREE_MB=$(numa_free_mb "$NUMA_NODE") || die "cannot read NUMA node $NUMA_NODE free memory" 2
MEM_TOTAL=$(free -g 2>/dev/null | awk 'NR == 2 {print $2}' || true)
MEM_FREE=$(free -g 2>/dev/null | awk 'NR == 2 {print $4}' || true)
DISK_MODEL=$(lsblk -d -o NAME,TYPE,SIZE,ROTA,MODEL 2>/dev/null | awk 'NR > 1 && $2 == "disk" && $1 !~ /^nbd/ {print $5; exit}' || true)
[[ -n "$DISK_MODEL" ]] || DISK_MODEL="unknown"

{
  printf 'OS=%s\n' "$OS"
  printf 'KERNEL=%s\n' "$KERNEL"
  printf 'ARCH=%s\n' "$ARCH"
  printf 'CPU_MODEL=%s\n' "$CPU_MODEL"
  printf 'CPU_LIST=%s\n' "$CPU_LIST"
  printf 'NUMA_NODE=%s\n' "$NUMA_NODE"
  printf 'NUMA_FREE_MB=%s\n' "$NUMA_FREE_MB"
  printf 'DISK_AVAIL_KB=%s\n' "$avail_kb"
  printf 'DISK_MODEL=%s\n' "$DISK_MODEL"
  printf 'MEM_TOTAL=%s\n' "$MEM_TOTAL"
  printf 'MEM_FREE=%s\n' "$MEM_FREE"
} > "$RESULT_DIR/env/preflight.env"

{
  printf '=== OS ===\n'; cat /etc/os-release 2>/dev/null || true
  printf '\n=== uname ===\n'; uname -a 2>/dev/null || true
  printf '\n=== CPU / NUMA ===\n'; numactl -H 2>/dev/null || true; lscpu 2>/dev/null || true
  printf '\n=== memory ===\n'; free -h 2>/dev/null || true
  printf '\n=== HugePages / THP ===\n'; grep -iE 'HugePages|Hugepagesize' /proc/meminfo 2>/dev/null || true
  cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || true
  printf '\n=== disk ===\n'; lsblk -o NAME,TYPE,SIZE,ROTA,MODEL,MOUNTPOINT 2>/dev/null | grep -v nbd || true
  findmnt -T "$DATA_DIR" 2>/dev/null || true
  df -hT "$DATA_DIR" 2>/dev/null || true
  df -i "$DATA_DIR" 2>/dev/null || true
} > "$RESULT_DIR/env/preflight.txt" 2>&1

log_info "preflight OK: arch=$ARCH node=$NUMA_NODE free=${NUMA_FREE_MB}MB disk_avail=${avail_kb}KB"
