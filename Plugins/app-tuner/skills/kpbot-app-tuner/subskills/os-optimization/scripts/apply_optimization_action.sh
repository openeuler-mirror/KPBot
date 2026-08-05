#!/usr/bin/env bash
set -euo pipefail

# apply_optimization_action.sh — OS optimization action executor
# Supports: sysctl, thp, governor, hugepages, numa-balancing, irqbalance,
#           io-scheduler, ulimit, tuned, grub-params, sched-feature
# Common options: --persist, --audit-log, --dry-run/--execute, --rollback

ACTION=""
KEY=""
VALUE=""
PID=""
CPUS=""
IFACE=""
PORT="3306"
DEVICE=""
LIMIT_NAME=""
NUMA_NODE=""
PROFILE=""
GRUB_PARAMS=""
SCHED_FEATURE=""
IRQ_ACTION=""
BACKUP_DIR="./os-optimization-output/rollback"
AUDIT_LOG="./os-optimization-output/audit_log.jsonl"
EXECUTE=false
APPROVED_CHANGE_ID=""
PERSIST=false
NO_PERSIST=false
ROLLBACK_MODE=false
ROLLBACK_ALL=false

usage() {
  cat <<'EOF'
Usage:
  apply_optimization_action.sh --action <name> [options]
  apply_optimization_action.sh --rollback [--backup-dir <dir>] [--rollback-all]

Actions:
  sysctl              --key <name> --value <value>
  thp                 --value always|madvise|never
  governor            --value <governor>
  hugepages           --value <nr> [--numa-node <node>]
  numa-balancing      --value 0|1
  irqbalance          --irq-action stop|disable
  io-scheduler        --device <dev> --value none|mq-deadline|kyber
  ulimit              --pid <pid> --limit nofile|nproc --value <n>
  tuned               --profile <profile>
  grub-params         --params "kpti=off mitigation=off"
  sched-feature       --feature STEAL --value enable|disable
  taskset-advice      --pid <pid> --cpus <cpu-list>
  optimize-network    --iface <iface> --cpus <cpu-list> [--port <port>]

Common Options:
  --persist               Persist changes (write to config files)
  --no-persist            Explicitly disable persistence
  --audit-log <path>      Audit log path (default: ./os-optimization-output/audit_log.jsonl)
  --dry-run               Only preview commands (default)
  --execute               Apply the change (requires --approved-change-id)
  --approved-change-id <id>  Required with --execute
  --backup-dir <dir>      Backup and rollback directory
  -h, --help              Show help

Rollback:
  --rollback              Rollback last batch
  --rollback-all          Rollback all batches (reverse order)
EOF
}

fail() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action) ACTION="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --value) VALUE="$2"; shift 2 ;;
    --pid) PID="$2"; shift 2 ;;
    --cpus|--app-cpus) CPUS="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --limit) LIMIT_NAME="$2"; shift 2 ;;
    --numa-node) NUMA_NODE="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --params) GRUB_PARAMS="$2"; shift 2 ;;
    --feature) SCHED_FEATURE="$2"; shift 2 ;;
    --irq-action) IRQ_ACTION="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --audit-log) AUDIT_LOG="$2"; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    --approved-change-id) APPROVED_CHANGE_ID="$2"; shift 2 ;;
    --persist) PERSIST=true; shift ;;
    --no-persist) NO_PERSIST=true; shift ;;
    --rollback) ROLLBACK_MODE=true; shift ;;
    --rollback-all) ROLLBACK_ALL=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

# ── Rollback mode ────────────────────────────────────────────
if [[ "${ROLLBACK_MODE}" == true || "${ROLLBACK_ALL}" == true ]]; then
  mkdir -p "${BACKUP_DIR}"
  if [[ "${ROLLBACK_ALL}" == true ]]; then
    snapshots=($(ls -1 "${BACKUP_DIR}"/batch_*_rollback.json 2>/dev/null | sort -r))
  else
    snapshots=($(ls -1 "${BACKUP_DIR}"/batch_*_rollback.json 2>/dev/null | sort -r | head -1))
  fi
  if [[ ${#snapshots[@]} -eq 0 ]]; then
    echo "No rollback snapshots found in ${BACKUP_DIR}"
    exit 0
  fi
  echo "=== Rollback ==="
  for snap in "${snapshots[@]}"; do
    echo "Processing: ${snap}"
    python3 - "${snap}" <<'PYEOF'
import json, subprocess, sys
snap = json.loads(open(sys.argv[1]).read())
for p in snap.get("params", []):
    cmd = p.get("rollback_cmd", "")
    if cmd:
        print(f"  Rollback: {p['name']}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        status = "success" if result.returncode == 0 else f"failed(exit={result.returncode})"
        print(f"    {status}")
        if result.stderr:
            print(f"    stderr: {result.stderr.strip()}")
PYEOF
  done
  echo "=== Rollback complete ==="
  exit 0
fi

# ── Normal execution ─────────────────────────────────────────
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

# ── Audit logging ────────────────────────────────────────────
write_audit() {
  local param="$1" old="$2" new="$3" mode="$4" persisted="$5"
  if [[ "${EXECUTE}" != true ]]; then return; fi
  local ts
  ts=$(date -Iseconds)
  python3 - "${AUDIT_LOG}" "${ts}" "${APPROVED_CHANGE_ID}" "${ACTION}" \
    "${param}" "${old}" "${new}" "${mode}" "${persisted}" <<'PYEOF'
import json, os, sys
log_path, ts, change_id, action, param, old, new, mode, persisted = sys.argv[1:11]
entry = {
    "timestamp": ts, "approved_change_id": change_id, "action": action,
    "param": param, "old_value": old, "new_value": new,
    "change_mode": mode, "persisted": persisted == "true",
    "executed_by": os.environ.get("USER", "unknown"), "status": "success"
}
os.makedirs(os.path.dirname(log_path), exist_ok=True)
with open(log_path, "a") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
PYEOF
}

# ── Persistence helpers ──────────────────────────────────────
persist_sysctl() {
  local key="$1" value="$2"
  local conf="/etc/sysctl.d/99-os-optimization.conf"
  if [[ "${PERSIST}" == true && "${NO_PERSIST}" == false ]]; then
    sed -i "/^${key}=/d" "${conf}" 2>/dev/null || true
    echo "${key}=${value}" >> "${conf}"
    sysctl -p "${conf}" 2>/dev/null || true
    echo "  [persist] ${key}=${value} → ${conf}"
  fi
}

persist_thp() {
  local value="$1"
  if [[ "${PERSIST}" == true && "${NO_PERSIST}" == false ]]; then
    local rcfile="/etc/rc.d/rc.local"
    sed -i "/transparent_hugepage\/enabled/d" "${rcfile}" 2>/dev/null || true
    sed -i "/transparent_hugepage\/defrag/d" "${rcfile}" 2>/dev/null || true
    echo "echo ${value} > /sys/kernel/mm/transparent_hugepage/enabled" >> "${rcfile}"
    echo "echo ${value} > /sys/kernel/mm/transparent_hugepage/defrag" >> "${rcfile}"
    chmod +x "${rcfile}" 2>/dev/null || true
    echo "  [persist] THP=${value} → ${rcfile}"
  fi
}

persist_ioscheduler() {
  local device="$1" value="$2"
  if [[ "${PERSIST}" == true && "${NO_PERSIST}" == false ]]; then
    local rule="/etc/udev/rules.d/60-io-scheduler.rules"
    echo "ACTION==\"add|change\", KERNEL==\"${device}\", ATTR{queue/scheduler}=\"${value}\"" > "${rule}"
    echo "  [persist] I/O scheduler ${device}=${value} → ${rule}"
  fi
}

# ── Actions ──────────────────────────────────────────────────

case "${ACTION}" in
  sysctl)
    [[ -n "${KEY}" && -n "${VALUE}" ]] || fail "sysctl requires --key and --value"
    current="$(sysctl -n "${KEY}" 2>/dev/null || true)"
    echo "sysctl ${KEY}: current='${current}' target='${VALUE}'"
    dry_run "sysctl -w ${KEY}=${VALUE}" && exit 0
    need_root_for_execute
    printf 'sysctl -w %q=%q\n' "${KEY}" "${current}" >> "${ROLLBACK_FILE}"
    sysctl -w "${KEY}=${VALUE}"
    persist_sysctl "${KEY}" "${VALUE}"
    write_audit "${KEY}" "${current}" "${VALUE}" "online" "${PERSIST}"
    ;;

  thp)
    [[ "${VALUE}" =~ ^(always|madvise|never)$ ]] || fail "thp requires --value always|madvise|never"
    THP_PATH="/sys/kernel/mm/transparent_hugepage/enabled"
    THP_DEFRAG="/sys/kernel/mm/transparent_hugepage/defrag"
    [[ -w "${THP_PATH}" || -r "${THP_PATH}" ]] || fail "THP path unavailable"
    current="$(cat "${THP_PATH}")"
    echo "THP current='${current}' target='${VALUE}'"
    dry_run "echo ${VALUE} > ${THP_PATH}" && exit 0
    need_root_for_execute
    previous="$(printf '%s\n' "${current}" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')"
    [[ -n "${previous}" ]] && printf 'echo %q > %q\n' "${previous}" "${THP_PATH}" >> "${ROLLBACK_FILE}"
    echo "${VALUE}" > "${THP_PATH}"
    [[ -w "${THP_DEFRAG}" ]] && echo "${VALUE}" > "${THP_DEFRAG}" 2>/dev/null || true
    persist_thp "${VALUE}"
    write_audit "THP" "${previous:-unknown}" "${VALUE}" "online" "${PERSIST}"
    ;;

  governor)
    [[ -n "${VALUE}" ]] || fail "governor requires --value"
    dry_run "set all CPU scaling_governor to ${VALUE}" && exit 0
    need_root_for_execute
    for path in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      [[ -f "${path}" ]] || continue
      current="$(cat "${path}")"
      printf 'echo %q > %q\n' "${current}" "${path}" >> "${ROLLBACK_FILE}"
      echo "${VALUE}" > "${path}"
    done
    if [[ "${PERSIST}" == true && "${NO_PERSIST}" == false ]] && command -v cpupower >/dev/null 2>&1; then
      cpupower frequency-set --governor "${VALUE}" 2>/dev/null || true
      echo "  [persist] governor=${VALUE} via cpupower"
    fi
    write_audit "governor" "${current:-unknown}" "${VALUE}" "online" "${PERSIST}"
    ;;

  hugepages)
    [[ -n "${VALUE}" ]] || fail "hugepages requires --value"
    # Memory limit check
    total_mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    page_size_kb=2048
    requested_kb=$(( VALUE * page_size_kb ))
    reserved_kb=$(( 4 * 1024 * 1024 ))  # 4GB
    remaining_kb=$(( total_mem_kb - requested_kb - reserved_kb ))
    if [[ ${remaining_kb} -lt 0 ]]; then
      fail "HugePages allocation ${VALUE}×${page_size_kb}KB exceeds available memory (remaining would be ${remaining_kb}KB)"
    elif [[ ${remaining_kb} -lt 0 ]]; then
      echo "WARNING: HugePages allocation leaves only ${remaining_kb}KB (< 4GB reserved). Proceed with caution."
    fi
    echo "HugePages: total_mem=${total_mem_kb}KB, requested=${requested_kb}KB, remaining=${remaining_kb}KB"

    if [[ -n "${NUMA_NODE}" ]]; then
      hp_path="/sys/devices/system/node/node${NUMA_NODE}/hugepages/hugepages-2048kB/nr_hugepages"
      dry_run "echo ${VALUE} > ${hp_path}" && exit 0
      need_root_for_execute
      current="$(cat "${hp_path}" 2>/dev/null || echo 0)"
      printf 'echo %q > %q\n' "${current}" "${hp_path}" >> "${ROLLBACK_FILE}"
      echo "${VALUE}" > "${hp_path}"
      write_audit "hugepages.node${NUMA_NODE}" "${current}" "${VALUE}" "restart_required" "${PERSIST}"
    else
      dry_run "sysctl -w vm.nr_hugepages=${VALUE}" && exit 0
      need_root_for_execute
      current="$(sysctl -n vm.nr_hugepages 2>/dev/null || echo 0)"
      printf 'sysctl -w vm.nr_hugepages=%q\n' "${current}" >> "${ROLLBACK_FILE}"
      sysctl -w "vm.nr_hugepages=${VALUE}"
      persist_sysctl "vm.nr_hugepages" "${VALUE}"
      write_audit "vm.nr_hugepages" "${current}" "${VALUE}" "restart_required" "${PERSIST}"
    fi
    ;;

  numa-balancing)
    [[ "${VALUE}" =~ ^(0|1)$ ]] || fail "numa-balancing requires --value 0 or 1"
    current="$(cat /proc/sys/kernel/numa_balancing 2>/dev/null || echo unknown)"
    echo "numa_balancing: current='${current}' target='${VALUE}'"
    dry_run "echo ${VALUE} > /proc/sys/kernel/numa_balancing" && exit 0
    need_root_for_execute
    printf 'echo %q > /proc/sys/kernel/numa_balancing\n' "${current}" >> "${ROLLBACK_FILE}"
    echo "${VALUE}" > /proc/sys/kernel/numa_balancing
    persist_sysctl "kernel.numa_balancing" "${VALUE}"
    write_audit "kernel.numa_balancing" "${current}" "${VALUE}" "online" "${PERSIST}"
    ;;

  irqbalance)
    [[ -n "${IRQ_ACTION}" ]] || fail "irqbalance requires --irq-action stop|disable"
    current="$(systemctl is-active irqbalance 2>/dev/null || echo unknown)"
    echo "irqbalance: current='${current}' action='${IRQ_ACTION}'"
    case "${IRQ_ACTION}" in
      stop) dry_run "systemctl stop irqbalance" && exit 0; need_root_for_execute ;;
      disable) dry_run "systemctl stop irqbalance && systemctl disable irqbalance" && exit 0; need_root_for_execute ;;
      *) fail "irqbalance --irq-action must be stop or disable" ;;
    esac
    printf 'systemctl start irqbalance\n' >> "${ROLLBACK_FILE}"
    [[ "${IRQ_ACTION}" == "disable" ]] && printf 'systemctl enable irqbalance\n' >> "${ROLLBACK_FILE}"
    systemctl stop irqbalance 2>/dev/null || true
    [[ "${IRQ_ACTION}" == "disable" ]] && systemctl disable irqbalance 2>/dev/null || true
    write_audit "irqbalance" "${current}" "${IRQ_ACTION}" "online" "true"
    ;;

  io-scheduler)
    [[ -n "${DEVICE}" && -n "${VALUE}" ]] || fail "io-scheduler requires --device and --value"
    sched_path="/sys/block/${DEVICE}/queue/scheduler"
    [[ -r "${sched_path}" ]] || fail "scheduler path unavailable: ${sched_path}"
    current="$(cat "${sched_path}")"
    echo "I/O scheduler ${DEVICE}: current='${current}' target='${VALUE}'"
    dry_run "echo ${VALUE} > ${sched_path}" && exit 0
    need_root_for_execute
    previous="$(printf '%s\n' "${current}" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')"
    [[ -n "${previous}" ]] && printf 'echo %q > %q\n' "${previous}" "${sched_path}" >> "${ROLLBACK_FILE}"
    echo "${VALUE}" > "${sched_path}"
    persist_ioscheduler "${DEVICE}" "${VALUE}"
    write_audit "io-scheduler.${DEVICE}" "${previous:-unknown}" "${VALUE}" "online" "${PERSIST}"
    ;;

  ulimit)
    [[ -n "${PID}" && -n "${LIMIT_NAME}" && -n "${VALUE}" ]] || fail "ulimit requires --pid, --limit, --value"
    [[ -r "/proc/${PID}/limits" ]] || fail "PID ${PID} not accessible"
    current="$(grep -i "Max ${LIMIT_NAME}" "/proc/${PID}/limits" 2>/dev/null | head -1 || echo unknown)"
    echo "ulimit ${LIMIT_NAME}: current='${current}' target='${VALUE}'"
    echo "  Note: ulimit changes require modifying systemd unit or limits.conf + service restart"
    dry_run "Modify Limit${LIMIT_NAME} in systemd unit → systemctl daemon-reload → restart service" && exit 0
    need_root_for_execute
    echo "  Manual step required: edit systemd unit or /etc/security/limits.conf"
    printf 'ulimit manual revert required (restore original Limit%s)\n' "${LIMIT_NAME}" >> "${ROLLBACK_FILE}"
    write_audit "ulimit.${LIMIT_NAME}" "${current}" "${VALUE}" "restart_required" "${PERSIST}"
    ;;

  tuned)
    [[ -n "${PROFILE}" ]] || fail "tuned requires --profile"
    current="$(tuned-adm active 2>/dev/null | head -1 || echo unknown)"
    echo "tuned: current='${current}' target='${PROFILE}'"
    dry_run "tuned-adm profile ${PROFILE}" && exit 0
    need_root_for_execute
    printf 'tuned-adm profile %s\n' "${current##* }" >> "${ROLLBACK_FILE}" 2>/dev/null || true
    tuned-adm profile "${PROFILE}" 2>/dev/null || fail "tuned-adm profile failed"
    write_audit "tuned" "${current}" "${PROFILE}" "online" "true"
    ;;

  grub-params)
    [[ -n "${GRUB_PARAMS}" ]] || fail "grub-params requires --params"
    echo "GRUB params: ${GRUB_PARAMS}"
    echo "  ⚠️  This action modifies /etc/default/grub and requires system reboot"
    # Safety check
    if echo "${GRUB_PARAMS}" | grep -q "kpti=off"; then
      echo "  ⚠️  kpti=off: 关闭内核页表隔离,生产环境需评估安全风险"
    fi
    if echo "${GRUB_PARAMS}" | grep -q "mitigation=off"; then
      echo "  ⚠️  mitigation=off: 关闭 Spectre 缓解,鲲鹏不受 Meltdown 影响但受 Spectre 影响"
    fi
    dry_run "Modify /etc/default/grub + grub2-mkconfig" && exit 0
    need_root_for_execute
    # Backup grub config
    cp /etc/default/grub "/etc/default/grub.bak.$(date +%s)"
    # Add params to GRUB_CMDLINE_LINUX
    for param in ${GRUB_PARAMS}; do
      if ! grep -q "${param}" /etc/default/grub; then
        sed -i "s/GRUB_CMDLINE_LINUX=\"/&${param} /" /etc/default/grub
      fi
    done
    # Regenerate grub config
    if [[ -f /boot/efi/EFI/openEuler/grub.cfg ]]; then
      grub2-mkconfig -o /boot/efi/EFI/openEuler/grub.cfg
    elif [[ -f /boot/grub2/grub.cfg ]]; then
      grub2-mkconfig -o /boot/grub2/grub.cfg
    else
      echo "  ⚠️  grub.cfg location not found, manual grub2-mkconfig required"
    fi
    printf 'Restore /etc/default/grub.bak.* + grub2-mkconfig (requires reboot)\n' >> "${ROLLBACK_FILE}"
    echo "  GRUB params modified. System reboot required to take effect."
    write_audit "GRUB" "original" "${GRUB_PARAMS}" "system_reboot" "true"
    ;;

  sched-feature)
    [[ -n "${SCHED_FEATURE}" && -n "${VALUE}" ]] || fail "sched-feature requires --feature and --value"
    sched_path="/sys/kernel/debug/sched_features"
    [[ -r "${sched_path}" ]] || fail "sched_features path unavailable (debugfs not mounted?)"
    if [[ "${VALUE}" == "enable" ]]; then
      dry_run "echo ${SCHED_FEATURE} > ${sched_path}" && exit 0
      need_root_for_execute
      printf 'echo NO_%s > %q\n' "${SCHED_FEATURE}" "${sched_path}" >> "${ROLLBACK_FILE}"
      echo "${SCHED_FEATURE}" > "${sched_path}"
    elif [[ "${VALUE}" == "disable" ]]; then
      dry_run "echo NO_${SCHED_FEATURE} > ${sched_path}" && exit 0
      need_root_for_execute
      printf 'echo %s > %q\n' "${SCHED_FEATURE}" "${sched_path}" >> "${ROLLBACK_FILE}"
      echo "NO_${SCHED_FEATURE}" > "${sched_path}"
    else
      fail "sched-feature --value must be enable or disable"
    fi
    write_audit "sched_feature.${SCHED_FEATURE}" "previous" "${VALUE}" "online" "false"
    ;;

  taskset-advice)
    [[ -n "${PID}" && -n "${CPUS}" ]] || fail "taskset-advice requires --pid and --cpus"
    echo "recommended command: taskset -pc ${CPUS} ${PID}"
    echo "rollback: record previous affinity with taskset -pc ${PID} before applying"
    ;;

  optimize-network)
    [[ -n "${IFACE}" && -n "${CPUS}" ]] || fail "optimize-network requires --iface and --cpus"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    MAIN_SCRIPTS="${SCRIPT_DIR}/../../scripts"
    cmd=("${MAIN_SCRIPTS}/optimize_network.sh" --iface "${IFACE}" --app-cpus "${CPUS}" --port "${PORT}")
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
