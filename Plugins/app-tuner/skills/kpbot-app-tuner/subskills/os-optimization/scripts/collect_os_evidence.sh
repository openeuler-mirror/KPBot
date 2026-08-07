#!/usr/bin/env bash
set -euo pipefail

# collect_os_evidence.sh
# OS 调优证据采集脚本,支持三种模式:
#   --full        全量采集(独立运行)
#   --supplement  增量补采(主SKLL证据不足时补采缺失项)
#   --check-only  只检查完整性,不采集

usage() {
  cat <<'EOF'
Usage:
  collect_os_evidence.sh --full --output-dir <dir> [options]
  collect_os_evidence.sh --supplement --output-dir <dir> [options]
  collect_os_evidence.sh --check-only [options]

Modes:
  --full        Collect all required OS tuning evidence (standalone mode)
  --supplement  Check existing evidence and collect only missing items
  --check-only  Check completeness only, output missing list, do not collect

Options:
  --output-dir <dir>            Output directory for collected evidence
  --existing-dir <dir>          Existing evidence_snapshot_dir (supplement/check mode)
  --existing-backup-dir <dir>   Existing environment_backup_dir (supplement/check mode)
  --target-pid <pid>            Target process PID (for /proc/<pid>/limits)
  --db-type <type>              Database type: mysql|postgresql|redis|mongodb
  --db-conn <conn>              Database connection string
  -h, --help                    Show help

Examples:
  # Standalone full collection
  collect_os_evidence.sh --full --output-dir ./os-evidence \
    --target-pid 12345 --db-type mysql --db-conn "mysql -h 127.0.0.1 -P 3306 -u root"

  # Supplement missing items from main SKILL evidence
  collect_os_evidence.sh --supplement --output-dir ./os-evidence \
    --existing-dir /path/to/evidence_snapshot \
    --existing-backup-dir /path/to/environment_backup \
    --target-pid 12345 --db-type postgresql --db-conn "psql -h localhost"

  # Check only
  collect_os_evidence.sh --check-only \
    --existing-dir /path/to/evidence_snapshot \
    --existing-backup-dir /path/to/environment_backup
EOF
}

MODE=""
OUTPUT_DIR=""
EXISTING_DIR=""
EXISTING_BACKUP_DIR=""
TARGET_PID=""
DB_TYPE=""
DB_CONN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --supplement) MODE="supplement"; shift ;;
    --check-only) MODE="check_only"; shift ;;
    --output-dir) OUTPUT_DIR="${2:?}"; shift 2 ;;
    --existing-dir) EXISTING_DIR="${2:?}"; shift 2 ;;
    --existing-backup-dir) EXISTING_BACKUP_DIR="${2:?}"; shift 2 ;;
    --target-pid) TARGET_PID="${2:?}"; shift 2 ;;
    --db-type) DB_TYPE="${2:?}"; shift 2 ;;
    --db-conn) DB_CONN="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -n "${MODE}" ]] || { echo "ERROR: mode (--full/--supplement/--check-only) is required" >&2; usage >&2; exit 1; }

if [[ "${MODE}" != "check_only" ]]; then
  [[ -n "${OUTPUT_DIR}" ]] || { echo "ERROR: --output-dir is required for ${MODE} mode" >&2; exit 1; }
fi

# ── Helper functions ──────────────────────────────────────────

timestamp() { date -Iseconds; }

run_capture() {
  local file="$1" label="$2"; shift 2
  {
    printf '===== %s =====\n' "${label}"
    printf 'time: %s\n' "$(timestamp)"
    printf 'command: %s\n' "$*"
  } > "${file}"
  if "$@" >> "${file}" 2>&1; then
    printf '\n' >> "${file}"
    echo "  ✅ ${label}"
  else
    local rc=$?
    printf '\n[command failed with exit code %s]\n\n' "${rc}" >> "${file}"
    echo "  ⚠️  ${label} (failed, exit ${rc})"
  fi
}

run_capture_shell() {
  local file="$1" label="$2" cmd="$3"
  {
    printf '===== %s =====\n' "${label}"
    printf 'time: %s\n' "$(timestamp)"
    printf 'command: %s\n' "${cmd}"
  } > "${file}"
  if sh -c "${cmd}" >> "${file}" 2>&1; then
    printf '\n' >> "${file}"
    echo "  ✅ ${label}"
  else
    local rc=$?
    printf '\n[command failed with exit code %s]\n\n' "${rc}" >> "${file}"
    echo "  ⚠️  ${label} (failed, exit ${rc})"
  fi
}

capture_if_available() {
  local file="$1" label="$2" cmd_name="$3"; shift 3
  if command -v "${cmd_name}" >/dev/null 2>&1; then
    run_capture "${file}" "${label}" "$@"
  else
    echo "  ⚠️  ${label} (command not found: ${cmd_name})"
    printf '===== %s =====\n[command not found: %s]\n' "${label}" "${cmd_name}" > "${file}"
  fi
}

# Check if a file exists in existing evidence dirs
find_in_existing() {
  local filename="$1"
  if [[ -n "${EXISTING_DIR}" ]]; then
    local found
    found=$(find "${EXISTING_DIR}" -name "${filename}" -type f 2>/dev/null | head -1)
    [[ -n "${found}" ]] && { echo "${found}"; return 0; }
  fi
  if [[ -n "${EXISTING_BACKUP_DIR}" ]]; then
    local found
    found=$(find "${EXISTING_BACKUP_DIR}" -name "${filename}" -type f 2>/dev/null | head -1)
    [[ -n "${found}" ]] && { echo "${found}"; return 0; }
  fi
  return 1
}

# Search for evidence in existing dirs by pattern (grep content)
find_in_existing_content() {
  local pattern="$1" dir="$2"
  [[ -z "${dir}" ]] && return 1
  local found
  found=$(grep -rl "${pattern}" "${dir}" 2>/dev/null | head -1)
  [[ -n "${found}" ]] && { echo "${found}"; return 0; }
  return 1
}

# ── Evidence item definitions ────────────────────────────────
# Each item: name|required|check_fn|collect_fn|description

# Required evidence items (missing → degraded)
REQUIRED_ITEMS=(
  "cpu_platform"
  "os_version"
  "kernel_version"
  "sysctl_all"
  "governor"
  "thp_status"
  "hugepages_status"
  "meminfo"
  "numa_balancing"
  "environment_type"
  "io_scheduler"
  "mount_options"
  "irqbalance_status"
  "cmdline"
)

# Conditionally required (missing → specific param skip, not full degrade)
CONDITIONAL_ITEMS=(
  "numa_distance"
  "process_limits"
  "app_config"
)

# Optional (missing → no degrade, skip check)
OPTIONAL_ITEMS=(
  "tuned_status"
  "atune_status"
)

# ── Full mode collection ─────────────────────────────────────

collect_full() {
  echo "=== Full evidence collection ==="
  echo "Output: ${OUTPUT_DIR}"
  echo ""

  mkdir -p "${OUTPUT_DIR}"

  echo "--- Platform & Kernel ---"
  capture_if_available "${OUTPUT_DIR}/lscpu.txt" "CPU platform (lscpu)" lscpu lscpu
  run_capture_shell "${OUTPUT_DIR}/os-release.txt" "OS version" "cat /etc/os-release"
  run_capture_shell "${OUTPUT_DIR}/uname.txt" "Kernel version" "uname -a"

  echo ""
  echo "--- sysctl & CPU ---"
  capture_if_available "${OUTPUT_DIR}/sysctl.txt" "sysctl -a" sysctl sysctl -a
  run_capture_shell "${OUTPUT_DIR}/governor.txt" "CPU governor" \
    "for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do [ -r \"\$cpu\" ] && printf '%s: %s\n' \"\$(basename \$(dirname \$cpu))\" \"\$(cat \$cpu)\"; done"

  echo ""
  echo "--- Memory & THP & HugePages ---"
  run_capture_shell "${OUTPUT_DIR}/meminfo.txt" "/proc/meminfo" "cat /proc/meminfo"
  run_capture_shell "${OUTPUT_DIR}/thp_status.txt" "THP enabled/defrag" \
    "echo '=== enabled ==='; cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null || echo 'not available'; echo '=== defrag ==='; cat /sys/kernel/mm/transparent_hugepage/defrag 2>/dev/null || echo 'not available'"
  run_capture_shell "${OUTPUT_DIR}/hugepages_status.txt" "HugePages status" \
    "grep -E 'Huge|AnonHuge' /proc/meminfo"
  run_capture_shell "${OUTPUT_DIR}/numa_balancing.txt" "numa_balancing" \
    "cat /proc/sys/kernel/numa_balancing"

  echo ""
  echo "--- NUMA ---"
  if command -v numactl >/dev/null 2>&1; then
    run_capture "${OUTPUT_DIR}/numa_distance.txt" "NUMA topology (numactl -H)" numactl -H
  else
    echo "  ⚠️  NUMA topology (numactl not installed)"
    printf '===== NUMA topology =====\n[command not found: numactl]\n' > "${OUTPUT_DIR}/numa_distance.txt"
    # Fallback: try lscpu NUMA info
    run_capture_shell "${OUTPUT_DIR}/numa_distance.txt" "NUMA topology (lscpu fallback)" \
      "lscpu | grep -i numa" 2>/dev/null || true
  fi

  echo ""
  echo "--- Environment ---"
  detect_environment_type "${OUTPUT_DIR}"
  run_capture_shell "${OUTPUT_DIR}/container_limits.txt" "Container limits" \
    "if [ -f /proc/1/cgroup ]; then cat /proc/1/cgroup; elif [ -f /proc/self/cgroup ]; then cat /proc/self/cgroup; else echo 'no cgroup info'; fi"

  echo ""
  echo "--- I/O & Mount ---"
  run_capture_shell "${OUTPUT_DIR}/io_scheduler.txt" "I/O scheduler & nr_requests" \
    "for d in /sys/block/nvme* /sys/block/sd* /sys/block/vd* /sys/block/xvd*; do [ -d \"\$d/queue\" ] || continue; echo \"## \$(basename \$d)\"; for f in rotational scheduler nr_requests read_ahead_kb logical_block_size; do [ -r \"\$d/queue/\$f\" ] && printf '%s=%s\n' \"\$f\" \"\$(cat \"\$d/queue/\$f\")\"; done; done"
  run_capture "${OUTPUT_DIR}/mount.txt" "Mount options" mount

  echo ""
  echo "--- Interrupts & tuned & GRUB ---"
  capture_if_available "${OUTPUT_DIR}/irqbalance.txt" "irqbalance status" systemctl systemctl status irqbalance --no-pager 2>/dev/null || true
  capture_if_available "${OUTPUT_DIR}/tuned_status.txt" "tuned profile" tuned-adm tuned-adm active 2>/dev/null || true
  capture_if_available "${OUTPUT_DIR}/atune_status.txt" "A-Tune status" systemctl systemctl status atuned --no-pager 2>/dev/null || true
  run_capture_shell "${OUTPUT_DIR}/cmdline.txt" "Kernel cmdline" "cat /proc/cmdline"

  echo ""
  echo "--- Process limits ---"
  if [[ -n "${TARGET_PID}" ]]; then
    if [[ -r "/proc/${TARGET_PID}/limits" ]]; then
      run_capture "${OUTPUT_DIR}/process_limits.txt" "Process limits (PID ${TARGET_PID})" cat "/proc/${TARGET_PID}/limits"
    else
      echo "  ⚠️  Process limits (PID ${TARGET_PID} not accessible)"
      printf '===== Process limits =====\n[PID %s not accessible or does not exist]\n' "${TARGET_PID}" > "${OUTPUT_DIR}/process_limits.txt"
    fi
  else
    echo "  ⏭️  Process limits (no --target-pid specified, skipping)"
    printf '===== Process limits =====\n[no --target-pid specified]\n' > "${OUTPUT_DIR}/process_limits.txt"
  fi

  echo ""
  echo "--- Database config ---"
  collect_db_config "${OUTPUT_DIR}"

  echo ""
  echo "--- Collection complete ---"
  generate_manifest "full"
}

# ── Supplement mode ──────────────────────────────────────────

collect_supplement() {
  echo "=== Supplement evidence collection ==="
  echo "Existing evidence dir: ${EXISTING_DIR:-<none>}"
  echo "Existing backup dir: ${EXISTING_BACKUP_DIR:-<none>}"
  echo "Output: ${OUTPUT_DIR}"
  echo ""

  mkdir -p "${OUTPUT_DIR}"

  local collected_any=false

  # Check and collect each missing item
  echo "--- Checking missing items ---"

  # NUMA distance (always a gap in main SKILL)
  if ! find_in_existing "numa_distance.txt" >/dev/null 2>&1; then
    echo "  Missing: NUMA distance"
    if command -v numactl >/dev/null 2>&1; then
      run_capture "${OUTPUT_DIR}/numa_distance.txt" "NUMA topology (numactl -H)" numactl -H
      collected_any=true
    else
      echo "    ⚠️  numactl not installed, cannot collect"
      printf '===== NUMA topology =====\n[command not found: numactl]\n' > "${OUTPUT_DIR}/numa_distance.txt"
    fi
  else
    echo "  ✅ NUMA distance (found in existing)"
  fi

  # Process limits (always a gap)
  if [[ -n "${TARGET_PID}" ]]; then
    if ! find_in_existing "process_limits.txt" >/dev/null 2>&1; then
      echo "  Missing: Process limits"
      if [[ -r "/proc/${TARGET_PID}/limits" ]]; then
        run_capture "${OUTPUT_DIR}/process_limits.txt" "Process limits (PID ${TARGET_PID})" cat "/proc/${TARGET_PID}/limits"
        collected_any=true
      else
        echo "    ⚠️  PID ${TARGET_PID} not accessible"
        printf '===== Process limits =====\n[PID %s not accessible]\n' "${TARGET_PID}" > "${OUTPUT_DIR}/process_limits.txt"
      fi
    else
      echo "  ✅ Process limits (found in existing)"
    fi
  fi

  # Database config (gap for non-MySQL databases)
  if [[ -n "${DB_TYPE}" ]]; then
    local db_file
    case "${DB_TYPE}" in
      mysql) db_file="mysql_variables.txt" ;;
      postgresql) db_file="pg_settings.txt" ;;
      redis) db_file="redis_config.txt" ;;
      mongodb) db_file="mongo_config.txt" ;;
      *) db_file="" ;;
    esac
    if [[ -n "${db_file}" ]] && ! find_in_existing "${db_file}" >/dev/null 2>&1; then
      echo "  Missing: ${DB_TYPE} config (${db_file})"
      collect_db_config "${OUTPUT_DIR}"
      collected_any=true
    else
      echo "  ✅ ${DB_TYPE} config (found in existing)"
    fi
  fi

  if [[ "${collected_any}" == false ]]; then
    echo ""
    echo "  All required evidence already present. No supplement needed."
  fi

  echo ""
  generate_manifest "supplement"
}

# ── Check-only mode ──────────────────────────────────────────

check_only() {
  echo "=== Evidence completeness check ==="
  echo "Existing evidence dir: ${EXISTING_DIR:-<none>}"
  echo "Existing backup dir: ${EXISTING_BACKUP_DIR:-<none>}"
  echo ""
  generate_manifest "check_only"
}

# ── Environment detection ────────────────────────────────────

detect_environment_type() {
  local dir="$1"
  local env_type="baremetal"
  local virt="none"

  # Container detection
  if [[ -f /.dockerenv ]] || grep -qa docker /proc/1/cgroup 2>/dev/null; then
    env_type="container"
  elif [[ -f /proc/1/cgroup ]] && grep -qa 'containerd\|kubepods' /proc/1/cgroup 2>/dev/null; then
    env_type="container"
  fi

  # VM detection (if not container)
  if [[ "${env_type}" == "baremetal" ]]; then
    if [[ -f /proc/cpuinfo ]] && grep -qa 'hypervisor' /proc/cpuinfo 2>/dev/null; then
      env_type="vm"
      virt=$(grep -m1 'hypervisor' /proc/cpuinfo 2>/dev/null | head -1 || echo "unknown")
    elif command -v systemd-detect-virt >/dev/null 2>&1; then
      local detected
      detected=$(systemd-detect-virt 2>/dev/null || echo "none")
      if [[ "${detected}" != "none" && "${detected}" != "kvm" ]]; then
        env_type="vm"
        virt="${detected}"
      elif [[ "${detected}" == "kvm" ]]; then
        env_type="vm"
        virt="kvm"
      fi
    fi
  fi

  {
    printf '===== Environment Type =====\n'
    printf 'time: %s\n' "$(timestamp)"
    printf 'environment-type: %s\n' "${env_type}"
    printf 'virtualization: %s\n' "${virt}"
  } > "${dir}/environment-type.txt"
  echo "  ✅ Environment type (${env_type})"
}

# ── Database config collection ───────────────────────────────

collect_db_config() {
  local dir="$1"

  if [[ -z "${DB_TYPE}" ]]; then
    echo "  ⏭️  Database config (no --db-type specified, skipping)"
    return
  fi

  case "${DB_TYPE}" in
    mysql)
      if [[ -n "${DB_CONN}" ]]; then
        echo "  Collecting MySQL config..."
        run_capture_shell "${dir}/mysql_variables.txt" "MySQL variables" \
          "${DB_CONN} -e \"SHOW VARIABLES\"" 2>/dev/null || \
        echo "  ⚠️  MySQL config collection failed (check --db-conn)"
      else
        echo "  ⚠️  MySQL config (no --db-conn, trying default)"
        run_capture_shell "${dir}/mysql_variables.txt" "MySQL variables" \
          "mysql -h 127.0.0.1 -u root -e \"SHOW VARIABLES\"" 2>/dev/null || \
        echo "  ⚠️  MySQL config collection failed"
      fi
      ;;
    postgresql)
      if [[ -n "${DB_CONN}" ]]; then
        echo "  Collecting PostgreSQL config..."
        run_capture_shell "${dir}/pg_settings.txt" "PostgreSQL settings" \
          "${DB_CONN} -c \"SHOW shared_buffers; SHOW max_connections; SHOW huge_pages;\"" 2>/dev/null || \
        echo "  ⚠️  PostgreSQL config collection failed"
      else
        echo "  ⚠️  PostgreSQL config (no --db-conn, trying default)"
        run_capture_shell "${dir}/pg_settings.txt" "PostgreSQL settings" \
          "psql -c \"SHOW shared_buffers; SHOW max_connections; SHOW huge_pages;\"" 2>/dev/null || \
        echo "  ⚠️  PostgreSQL config collection failed"
      fi
      ;;
    redis)
      if [[ -n "${DB_CONN}" ]]; then
        echo "  Collecting Redis config..."
        run_capture_shell "${dir}/redis_config.txt" "Redis config" \
          "${DB_CONN} CONFIG GET maxmemory CONFIG GET maxclients" 2>/dev/null || \
        echo "  ⚠️  Redis config collection failed"
      else
        echo "  ⚠️  Redis config (no --db-conn, trying default)"
        run_capture_shell "${dir}/redis_config.txt" "Redis config" \
          "redis-cli CONFIG GET maxmemory CONFIG GET maxclients" 2>/dev/null || \
        echo "  ⚠️  Redis config collection failed"
      fi
      ;;
    mongodb)
      if [[ -n "${DB_CONN}" ]]; then
        echo "  Collecting MongoDB config..."
        run_capture_shell "${dir}/mongo_config.txt" "MongoDB config" \
          "${DB_CONN} --eval 'db.serverStatus().wiredTiger.cache; db.serverStatus().connections'" 2>/dev/null || \
        echo "  ⚠️  MongoDB config collection failed"
      else
        echo "  ⚠️  MongoDB config (no --db-conn, trying default)"
        run_capture_shell "${dir}/mongo_config.txt" "MongoDB config" \
          "mongosh --eval 'db.serverStatus().wiredTiger.cache; db.serverStatus().connections'" 2>/dev/null || \
        echo "  ⚠️  MongoDB config collection failed"
      fi
      ;;
    *)
      echo "  ⚠️  Unknown database type: ${DB_TYPE}"
      ;;
  esac
}

# ── Manifest generation ──────────────────────────────────────

generate_manifest() {
  local mode="$1"
  local manifest_path

  if [[ "${mode}" == "check_only" ]]; then
    manifest_path="${EXISTING_DIR:-./}/os_evidence_manifest.json"
  else
    manifest_path="${OUTPUT_DIR}/os_evidence_manifest.json"
  fi

  python3 - "${manifest_path}" "${mode}" "${OUTPUT_DIR}" "${EXISTING_DIR}" "${EXISTING_BACKUP_DIR}" \
    "${TARGET_PID}" "${DB_TYPE}" <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone

manifest_path, mode, output_dir, existing_dir, existing_backup_dir, target_pid, db_type = sys.argv[1:8]

def check_file(name, search_dirs):
    for d in search_dirs:
        if not d:
            continue
        for root, dirs, files in os.walk(d):
            if name in files:
                rel = os.path.join(root, name)
                return "collected", rel, d
    return "missing", None, None

def check_content(pattern, search_dirs):
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    with open(fp, errors='ignore') as fh:
                        if pattern in fh.read():
                            return "collected", fp, d
                except:
                    pass
    return "missing", None, None

search_dirs = [d for d in [output_dir, existing_dir, existing_backup_dir] if d and os.path.isdir(d)]

items = []

# Required items
required_checks = [
    ("cpu_platform", "lscpu.txt", None),
    ("os_version", "os-release.txt", "os-release"),
    ("kernel_version", "uname.txt", "kernel-config.txt"),
    ("sysctl_all", "sysctl.txt", "sysctl"),
    ("governor", "governor.txt", "scaling_governor"),
    ("thp_status", "thp_status.txt", "transparent_hugepage"),
    ("hugepages_status", "hugepages_status.txt", "HugePages"),
    ("meminfo", "meminfo.txt", "meminfo"),
    ("numa_balancing", "numa_balancing.txt", "numa_balancing"),
    ("environment_type", "environment-type.txt", "environment-type"),
    ("io_scheduler", "io_scheduler.txt", "scheduler"),
    ("mount_options", "mount.txt", "mount"),
    ("irqbalance_status", "irqbalance.txt", "irqbalance"),
    ("cmdline", "cmdline.txt", "cmdline"),
]

for name, filename, content_pattern in required_checks:
    status, path, source_dir = check_file(filename, search_dirs)
    if status == "missing" and content_pattern:
        status, path, source_dir = check_content(content_pattern, search_dirs)
    source = "self_collected" if source_dir == output_dir else ("main_skill" if source_dir in [existing_dir, existing_backup_dir] else "unavailable")
    items.append({
        "name": name,
        "status": status,
        "source": source,
        "file": os.path.basename(path) if path else None,
        "directory": source_dir or "",
        "required": True,
    })

# Conditional items
conditional_checks = [
    ("numa_distance", "numa_distance.txt", None),
    ("process_limits", "process_limits.txt", None),
]
for name, filename, _ in conditional_checks:
    status, path, source_dir = check_file(filename, search_dirs)
    source = "self_collected" if source_dir == output_dir else ("main_skill" if source_dir in [existing_dir, existing_backup_dir] else "unavailable")
    items.append({
        "name": name,
        "status": status,
        "source": source,
        "file": os.path.basename(path) if path else None,
        "directory": source_dir or "",
        "required": "conditional",
    })

# App config
if db_type:
    db_files = {
        "mysql": "mysql_variables.txt",
        "postgresql": "pg_settings.txt",
        "redis": "redis_config.txt",
        "mongodb": "mongo_config.txt",
    }
    db_file = db_files.get(db_type, "")
    if db_file:
        status, path, source_dir = check_file(db_file, search_dirs)
        source = "self_collected" if source_dir == output_dir else ("main_skill" if source_dir in [existing_dir, existing_backup_dir] else "unavailable")
        items.append({
            "name": "app_config",
            "status": status,
            "source": source,
            "file": os.path.basename(path) if path else None,
            "directory": source_dir or "",
            "required": "conditional",
        })

# Optional items
optional_checks = [
    ("tuned_status", "tuned_status.txt", "tuned-adm"),
    ("atune_status", "atune_status.txt", "atuned"),
]
for name, filename, content_pattern in optional_checks:
    status, path, source_dir = check_file(filename, search_dirs)
    if status == "missing" and content_pattern:
        status, path, source_dir = check_content(content_pattern, search_dirs)
    source = "self_collected" if source_dir == output_dir else ("main_skill" if source_dir in [existing_dir, existing_backup_dir] else "unavailable")
    items.append({
        "name": name,
        "status": status,
        "source": source,
        "file": os.path.basename(path) if path else None,
        "directory": source_dir or "",
        "required": False,
    })

missing_required = [i["name"] for i in items if i["status"] == "missing" and i["required"] is True]
missing_conditional = [i["name"] for i in items if i["status"] == "missing" and i["required"] == "conditional"]
missing_optional = [i["name"] for i in items if i["status"] == "missing" and i["required"] is False]

if not missing_required and not missing_conditional:
    completeness = "complete"
elif len(missing_required) >= 3:
    completeness = "insufficient"
else:
    completeness = "partial"

manifest = {
    "mode": mode,
    "collected_at": datetime.now(timezone.utc).isoformat(),
    "target_pid": int(target_pid) if target_pid else None,
    "db_type": db_type or None,
    "evidence_items": items,
    "completeness": completeness,
    "missing_required": missing_required,
    "missing_conditional": missing_conditional,
    "missing_optional": missing_optional,
}

with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"\n=== Evidence Manifest ===")
print(f"  Path: {manifest_path}")
print(f"  Completeness: {completeness}")
print(f"  Missing required: {missing_required or 'none'}")
print(f"  Missing conditional: {missing_conditional or 'none'}")
print(f"  Missing optional: {missing_optional or 'none'}")
PYEOF
}

# ── Main ─────────────────────────────────────────────────────

case "${MODE}" in
  full)        collect_full ;;
  supplement)  collect_supplement ;;
  check_only)  check_only ;;
esac

echo ""
echo "=== Done ==="
