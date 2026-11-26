#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common_functions.sh"

PID="${1:-}"
OUTPUT_DIR="${2:-./output/cpu-affinity/diagnosis}"

mkdir -p "${OUTPUT_DIR}"

if [[ -z "${PID}" ]]; then
  write_json_error "target pid is required"
  exit 1
fi

if ! ps -p "${PID}" >/dev/null 2>&1; then
  write_json_error "pid ${PID} not found"
  exit 1
fi

OUT_TXT="${OUTPUT_DIR}/thread_affinity.txt"
OUT_JSON="${OUTPUT_DIR}/thread_affinity.json"

{
  echo "# Thread Affinity Snapshot"
  echo "pid=${PID}"
  echo "timestamp=$(date '+%Y-%m-%d %H:%M:%S')"
  echo
  if have_cmd taskset; then
    taskset -cp "${PID}" 2>&1 || true
    echo
    while read -r tid psr pcpu comm; do
      [[ -z "${tid}" ]] && continue
      affinity="$(taskset -pc "${tid}" 2>/dev/null | awk -F': ' 'END{print $2}' || true)"
      printf "tid=%s psr=%s pcpu=%s comm=%s affinity=%s\n" "${tid}" "${psr}" "${pcpu}" "${comm}" "${affinity:-unknown}"
    done < <(list_threads "${PID}")
  else
    echo "taskset missing"
  fi
} > "${OUT_TXT}"

if have_cmd taskset; then
  export TARGET_PID="${PID}"
  while read -r tid psr pcpu comm; do
    [[ -z "${tid}" ]] && continue
    affinity="$(taskset -pc "${tid}" 2>/dev/null | awk -F': ' 'END{print $2}' || true)"
    printf '%s|%s|%s|%s|%s\n' "${tid}" "${psr}" "${pcpu}" "${comm}" "${affinity:-unknown}"
  done < <(list_threads "${PID}") | awk -F'|' '
    BEGIN {printf "{\"target_pid\":\"%s\",\"thread_affinity\":[", ENVIRON["TARGET_PID"]; sep=""}
    {
      gsub(/"/,"\\\"",$4); gsub(/"/,"\\\"",$5);
      printf "%s{\"tid\":\"%s\",\"cpu\":\"%s\",\"pcpu\":\"%s\",\"comm\":\"%s\",\"affinity\":\"%s\"}", sep, $1, $2, $3, $4, $5;
      sep=","
    }
    END {printf "]}"}' > "${OUT_JSON}"
else
  cat > "${OUT_JSON}" <<EOF
{"target_pid":"${PID}","fallback_notes":["taskset_missing"],"thread_affinity":[]}
EOF
fi

cat "${OUT_JSON}"
