#!/usr/bin/env bash
set -euo pipefail

ACTION=""
KEY=""
VALUE=""
PID=""
CPUS=""
IFACE=""
PORT="3306"
BACKUP_DIR="./output/optimization-backups"
EXECUTE=false
APPROVED_CHANGE_ID=""

usage() {
  cat <<'EOF'
Usage:
  apply_optimization_action.sh --action <name> [options]

Actions:
  sysctl              Requires --key <name> --value <value>
  thp                 Requires --value always|madvise|never
  governor            Requires --value <governor>
  taskset-advice      Requires --pid <pid> --cpus <cpu-list>
  optimize-network    Requires --iface <iface> --cpus <cpu-list> [--port <port>]

Options:
  --execute           Apply the change. Default is dry-run.
  --approved-change-id <id>
                      Required with --execute. Records the user-approved change ticket/id.
  --backup-dir <dir>  Backup and rollback note directory.
  -h, --help          Show help.
EOF
}

fail() {
  echo "error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action) ACTION="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --value) VALUE="$2"; shift 2 ;;
    --pid) PID="$2"; shift 2 ;;
    --cpus|--app-cpus) CPUS="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    --approved-change-id) APPROVED_CHANGE_ID="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -n "${ACTION}" ]] || { usage >&2; fail "missing --action"; }
if [[ "${EXECUTE}" == true && -z "${APPROVED_CHANGE_ID}" ]]; then
  fail "--execute requires --approved-change-id"
fi

mkdir -p "${BACKUP_DIR}"
ROLLBACK_FILE="${BACKUP_DIR}/rollback_actions.sh"
touch "${ROLLBACK_FILE}"
chmod 700 "${ROLLBACK_FILE}"
if [[ "${EXECUTE}" == true ]]; then
  printf '# approved_change_id=%q\n' "${APPROVED_CHANGE_ID}" >> "${ROLLBACK_FILE}"
fi

need_root_for_execute() {
  if [[ "${EXECUTE}" == true && "$(id -u)" -ne 0 ]]; then
    fail "action ${ACTION} with --execute requires root"
  fi
}

dry_run() {
  if [[ "${EXECUTE}" != true ]]; then
    echo "[dry-run] $*"
    return 0
  fi
  return 1
}

case "${ACTION}" in
  sysctl)
    [[ -n "${KEY}" && -n "${VALUE}" ]] || fail "sysctl requires --key and --value"
    current="$(sysctl -n "${KEY}" 2>/dev/null || true)"
    echo "sysctl ${KEY}: current='${current}' target='${VALUE}'"
    dry_run "sysctl -w ${KEY}=${VALUE}" && exit 0
    need_root_for_execute
    printf 'sysctl -w %q=%q\n' "${KEY}" "${current}" >> "${ROLLBACK_FILE}"
    sysctl -w "${KEY}=${VALUE}"
    ;;
  thp)
    [[ "${VALUE}" =~ ^(always|madvise|never)$ ]] || fail "thp requires --value always|madvise|never"
    THP_PATH="/sys/kernel/mm/transparent_hugepage/enabled"
    [[ -w "${THP_PATH}" || -r "${THP_PATH}" ]] || fail "THP path unavailable: ${THP_PATH}"
    current="$(cat "${THP_PATH}")"
    echo "THP current='${current}' target='${VALUE}'"
    dry_run "echo ${VALUE} > ${THP_PATH}" && exit 0
    need_root_for_execute
    previous="$(printf '%s\n' "${current}" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')"
    [[ -n "${previous}" ]] && printf 'echo %q > %q\n' "${previous}" "${THP_PATH}" >> "${ROLLBACK_FILE}"
    echo "${VALUE}" > "${THP_PATH}"
    ;;
  governor)
    [[ -n "${VALUE}" ]] || fail "governor requires --value"
    dry_run "set all CPU scaling_governor files to ${VALUE}" && exit 0
    need_root_for_execute
    for path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      [[ -f "${path}" ]] || continue
      current="$(cat "${path}")"
      printf 'echo %q > %q\n' "${current}" "${path}" >> "${ROLLBACK_FILE}"
      echo "${VALUE}" > "${path}"
    done
    ;;
  taskset-advice)
    [[ -n "${PID}" && -n "${CPUS}" ]] || fail "taskset-advice requires --pid and --cpus"
    echo "recommended command: taskset -pc ${CPUS} ${PID}"
    echo "rollback: record previous affinity with taskset -pc ${PID} before applying"
    ;;
  optimize-network)
    [[ -n "${IFACE}" && -n "${CPUS}" ]] || fail "optimize-network requires --iface and --cpus"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cmd=("${SCRIPT_DIR}/optimize_network.sh" --iface "${IFACE}" --app-cpus "${CPUS}" --port "${PORT}")
    dry_run "${cmd[*]}" && exit 0
    need_root_for_execute
    cmd+=(--execute --approved-change-id "${APPROVED_CHANGE_ID}")
    "${cmd[@]}"
    ;;
  *)
    fail "unknown action: ${ACTION}"
    ;;
esac

echo "rollback notes: ${ROLLBACK_FILE}"
