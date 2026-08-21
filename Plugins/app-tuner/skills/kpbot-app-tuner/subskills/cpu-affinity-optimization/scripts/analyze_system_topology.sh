#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common_functions.sh"

OUTPUT_DIR="${1:-./output/cpu-affinity/diagnosis}"
TARGET_NIC="${2:-}"

mkdir -p "${OUTPUT_DIR}"

if have_cmd lscpu; then
  lscpu > "${OUTPUT_DIR}/cpu_info.txt"
else
  echo "lscpu missing" > "${OUTPUT_DIR}/cpu_info.txt"
fi

if have_cmd numactl; then
  numactl --hardware > "${OUTPUT_DIR}/numa_info.txt" 2>&1 || true
else
  echo "numactl missing" > "${OUTPUT_DIR}/numa_info.txt"
fi

if [[ -r /proc/interrupts ]]; then
  cp /proc/interrupts "${OUTPUT_DIR}/interrupt_info.txt"
else
  echo "/proc/interrupts unavailable" > "${OUTPUT_DIR}/interrupt_info.txt"
fi

if [[ -n "${TARGET_NIC}" ]]; then
  if have_cmd ethtool; then
    ethtool -i "${TARGET_NIC}" > "${OUTPUT_DIR}/nic_info.txt" 2>&1 || true
  else
    echo "ethtool missing" > "${OUTPUT_DIR}/nic_info.txt"
  fi
else
  echo "no target nic provided" > "${OUTPUT_DIR}/nic_info.txt"
fi

cat <<EOF
{
  "cpu_info": "${OUTPUT_DIR}/cpu_info.txt",
  "numa_info": "${OUTPUT_DIR}/numa_info.txt",
  "interrupt_info": "${OUTPUT_DIR}/interrupt_info.txt",
  "nic_info": "${OUTPUT_DIR}/nic_info.txt"
}
EOF
