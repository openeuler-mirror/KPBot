#!/usr/bin/env bash
set -euo pipefail

PID_LIST="${1:-}"
REPORT_FILE="${2:-./output/cpu-affinity/final/affinity_verification.md}"

mkdir -p "$(dirname "${REPORT_FILE}")"

if [[ -z "${PID_LIST}" ]]; then
  echo "missing target pid list" >&2
  exit 1
fi

PIDS="$(echo "${PID_LIST}" | tr ',' ' ')"

{
  echo "# CPU 亲和性验证报告"
  echo
  echo "- 生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "- 目标进程: ${PID_LIST}"
  echo
  for pid in ${PIDS}; do
    echo "## PID ${pid}"
    if ! ps -p "${pid}" >/dev/null 2>&1; then
      echo "- 状态: not_found"
      echo
      continue
    fi
    if command -v taskset >/dev/null 2>&1; then
      echo "### 主进程亲和性"
      echo '```text'
      taskset -cp "${pid}" 2>&1 || true
      echo '```'
    fi
    echo "### 线程视图"
    echo '```text'
    ps -T -p "${pid}" -o pid,tid,psr,pcpu,comm 2>/dev/null || true
    echo '```'
    if command -v numactl >/dev/null 2>&1; then
      echo "### NUMA 亲和性"
      echo '```text'
      numactl -p "${pid}" 2>&1 || true
      echo '```'
    fi
    echo
  done
} > "${REPORT_FILE}"

cat "${REPORT_FILE}"
