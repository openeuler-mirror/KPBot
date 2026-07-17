#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="environment-backup"
BMC_HOST="${BMC_HOST:-}"
BMC_USER="${BMC_USER:-}"
BMC_PASS="${BMC_PASS:-}"
NON_INTERACTIVE=0

usage() {
  cat <<'EOF'
Usage:
  backup_environment.sh [output-dir]
  backup_environment.sh --output-dir <dir> [--bmc-host <host>] [--bmc-user <user>] [--bmc-pass <pass>] [--non-interactive]

Environment variables:
  BMC_HOST / BMC_USER / BMC_PASS can be used instead of command line arguments.

Notes:
  Redfish BIOS collection is read-only and opt-in. If BMC credentials are not
  provided, the script skips Redfish and records that fact in bios-redfish.txt.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="${2:?missing value for --output-dir}"
      shift 2
      ;;
    --bmc-host)
      BMC_HOST="${2:?missing value for --bmc-host}"
      shift 2
      ;;
    --bmc-user)
      BMC_USER="${2:?missing value for --bmc-user}"
      shift 2
      ;;
    --bmc-pass)
      BMC_PASS="${2:?missing value for --bmc-pass}"
      shift 2
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      OUTPUT_DIR="$1"
      shift
      ;;
  esac
done

mkdir -p "${OUTPUT_DIR}"

MANIFEST_FILE="${OUTPUT_DIR}/command-manifest.txt"
TIMELINE_FILE="${OUTPUT_DIR}/optimization-timeline.txt"
REPORT_FILE="${OUTPUT_DIR}/environment-backup-report.html"

cat > "${OUTPUT_DIR}/README.txt" <<'EOF'
Environment backup artifacts collected by sequential read-only command execution.

The collector runs one command at a time and records:
- the command that was attempted
- whether it succeeded, failed, or was skipped
- the stdout/stderr captured for each target file

Collection groups:
- BIOS configuration: bios-info.txt, bios-redfish.txt, bios-redfish-*.json
- Hardware configuration: hardware-cpu.txt, hardware-memory.txt, hardware-disk.txt, hardware-nic.txt
- Software configuration: software-versions.txt, os-config.txt, kernel-config.txt, build-system.txt, perf-diagnosis.txt
- Runtime context: virtualization.txt, environment-type.txt, container-limits.txt
- Compatibility files: cpu-info.txt, numa-topology.txt, memory-info.txt, disk-info.txt, nic-info.txt, os-kernel.txt,
  compiler-runtime.txt, thp-status.txt, hugepages-status.txt
- Summary: environment-backup-report.html
- Audit trail: optimization-timeline.txt, command-manifest.txt
EOF

: > "${MANIFEST_FILE}"
: > "${TIMELINE_FILE}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log_manifest() {
  printf '[%s] %s\n' "$(timestamp)" "$*" >> "${MANIFEST_FILE}"
}

append_header() {
  local target_file="$1"
  local label="$2"
  {
    printf '===== %s =====\n' "${label}"
    printf 'time: %s\n' "$(timestamp)"
  } >> "${target_file}"
}

run_capture() {
  local target_file="$1"
  local label="$2"
  shift 2

  append_header "${target_file}" "${label}"
  log_manifest "RUN ${label}: $*"

  if "$@" >> "${target_file}" 2>&1; then
    printf '\n' >> "${target_file}"
    log_manifest "OK  ${label}"
  else
    local status=$?
    printf '\n[command failed with exit code %s]\n\n' "${status}" >> "${target_file}"
    log_manifest "FAIL ${label} exit=${status}"
  fi
}

run_capture_shell() {
  local target_file="$1"
  local label="$2"
  local command_text="$3"

  append_header "${target_file}" "${label}"
  log_manifest "RUN ${label}: ${command_text}"

  if sh -c "${command_text}" >> "${target_file}" 2>&1; then
    printf '\n' >> "${target_file}"
    log_manifest "OK  ${label}"
  else
    local status=$?
    printf '\n[command failed with exit code %s]\n\n' "${status}" >> "${target_file}"
    log_manifest "FAIL ${label} exit=${status}"
  fi
}

note_missing_command() {
  local target_file="$1"
  local command_name="$2"
  append_header "${target_file}" "missing-command:${command_name}"
  printf 'command not found: %s\n\n' "${command_name}" >> "${target_file}"
  log_manifest "SKIP missing-command:${command_name}"
}

capture_if_available() {
  local target_file="$1"
  local label="$2"
  local command_name="$3"
  shift 3

  if command -v "${command_name}" >/dev/null 2>&1; then
    run_capture "${target_file}" "${label}" "${command_name}" "$@"
  else
    note_missing_command "${target_file}" "${command_name}"
  fi
}

capture_shell_if_available() {
  local target_file="$1"
  local label="$2"
  local command_name="$3"
  local command_text="$4"

  if command -v "${command_name}" >/dev/null 2>&1; then
    run_capture_shell "${target_file}" "${label}" "${command_text}"
  else
    note_missing_command "${target_file}" "${command_name}"
  fi
}

redfish_get() {
  local endpoint="$1"
  local output_file="$2"

  if [[ -z "${BMC_HOST}" || -z "${BMC_USER}" || -z "${BMC_PASS}" ]]; then
    return 1
  fi

  if curl -k -sS --connect-timeout 10 --max-time 30 \
    -u "${BMC_USER}:${BMC_PASS}" \
    "https://${BMC_HOST}${endpoint}" \
    -H 'Accept: application/json' \
    -o "${output_file}"; then
    log_manifest "OK  redfish:${endpoint} -> ${output_file}"
    return 0
  fi

  log_manifest "FAIL redfish:${endpoint} -> ${output_file}"
  return 1
}

first_redfish_member() {
  local json_file="$1"
  python3 - "$json_file" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(errors="ignore"))
    members = data.get("Members") or []
    if members and isinstance(members[0], dict):
        print(members[0].get("@odata.id", ""))
except Exception:
    pass
PY
}

prompt_bmc_if_interactive() {
  if [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
    return
  fi
  if [[ -n "${BMC_HOST}" || ! -t 0 ]]; then
    return
  fi

  printf 'BMC/IPMI Redfish host for BIOS collection (blank to skip): ' >&2
  read -r BMC_HOST || BMC_HOST=""
  if [[ -z "${BMC_HOST}" ]]; then
    return
  fi
  printf 'BMC username: ' >&2
  read -r BMC_USER || BMC_USER=""
  printf 'BMC password: ' >&2
  stty -echo 2>/dev/null || true
  read -r BMC_PASS || BMC_PASS=""
  stty echo 2>/dev/null || true
  printf '\n' >&2
}

detect_environment() {
  local env_type="baremetal"
  local cpuset_value="unknown"
  local memlimit_value="unknown"
  local runtime_hint="none"

  if grep -qE 'docker|containerd|kubepods|podman' /proc/1/cgroup 2>/dev/null; then
    env_type="container"
    runtime_hint="$(grep -E 'docker|containerd|kubepods|podman' /proc/1/cgroup 2>/dev/null | head -n 1 || true)"

    if [[ -f /sys/fs/cgroup/cpuset.cpus.effective ]]; then
      cpuset_value="$(cat /sys/fs/cgroup/cpuset.cpus.effective 2>/dev/null || echo unknown)"
    elif [[ -f /sys/fs/cgroup/cpuset/cpuset.cpus ]]; then
      cpuset_value="$(cat /sys/fs/cgroup/cpuset/cpuset.cpus 2>/dev/null || echo unknown)"
    fi

    if [[ -f /sys/fs/cgroup/memory.max ]]; then
      memlimit_value="$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo unknown)"
    elif [[ -f /sys/fs/cgroup/memory/memory.limit_in_bytes ]]; then
      memlimit_value="$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo unknown)"
    fi
  elif command -v systemd-detect-virt >/dev/null 2>&1 && systemd-detect-virt 2>/dev/null | grep -qv '^none$'; then
    env_type="vm"
    runtime_hint="$(systemd-detect-virt 2>/dev/null || echo vm)"
  fi

  cat > "${OUTPUT_DIR}/environment-type.txt" <<EOF
ENV_TYPE=${env_type}
RUNTIME_HINT=${runtime_hint}
EOF

  cat > "${OUTPUT_DIR}/container-limits.txt" <<EOF
CPUSET=${cpuset_value}
MEMLIMIT=${memlimit_value}
EOF

  log_manifest "OK  environment-detection env_type=${env_type}"
}

collect_bios() {
  local bios_file="${OUTPUT_DIR}/bios-info.txt"
  local redfish_file="${OUTPUT_DIR}/bios-redfish.txt"

  capture_if_available "${bios_file}" "dmidecode-bios" dmidecode -t bios
  capture_if_available "${bios_file}" "dmidecode-system" dmidecode -t system
  capture_if_available "${bios_file}" "dmidecode-baseboard" dmidecode -t baseboard
  capture_if_available "${bios_file}" "system-profiler-hardware" system_profiler SPHardwareDataType
  capture_shell_if_available "${bios_file}" "sysfs-bios-version" cat "cat /sys/class/dmi/id/bios_version"
  capture_shell_if_available "${bios_file}" "sysfs-product" cat "for f in /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name /sys/class/dmi/id/product_version /sys/class/dmi/id/board_vendor /sys/class/dmi/id/board_name; do [ -r \"\$f\" ] && printf '%s=%s\n' \"\$f\" \"\$(cat \"\$f\")\"; done"

  : > "${redfish_file}"
  append_header "${redfish_file}" "redfish-bios-collection"
  if [[ -z "${BMC_HOST}" || -z "${BMC_USER}" || -z "${BMC_PASS}" ]]; then
    {
      printf 'status=skipped\n'
      printf 'reason=BMC host/user/password not provided\n'
      printf 'how_to_enable=pass --bmc-host/--bmc-user/--bmc-pass or set BMC_HOST/BMC_USER/BMC_PASS\n\n'
    } >> "${redfish_file}"
    log_manifest "SKIP redfish-bios missing-bmc-credentials"
    return
  fi

  {
    printf 'status=attempted\n'
    printf 'bmc_host=%s\n' "${BMC_HOST}"
    printf 'bmc_user=%s\n' "${BMC_USER}"
    printf 'bmc_pass=***redacted***\n\n'
  } >> "${redfish_file}"

  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl command not found; Redfish collection failed\n' >> "${redfish_file}"
    log_manifest "FAIL redfish-bios curl-missing"
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 command not found; Redfish member discovery skipped\n' >> "${redfish_file}"
    log_manifest "FAIL redfish-bios python3-missing"
    return
  fi

  redfish_get "/redfish/v1" "${OUTPUT_DIR}/bios-redfish-root.json" || true
  redfish_get "/redfish/v1/Systems" "${OUTPUT_DIR}/bios-redfish-systems.json" || true
  redfish_get "/redfish/v1/Managers" "${OUTPUT_DIR}/bios-redfish-managers.json" || true

  local system_endpoint=""
  system_endpoint="$(first_redfish_member "${OUTPUT_DIR}/bios-redfish-systems.json")"
  if [[ -n "${system_endpoint}" ]]; then
    redfish_get "${system_endpoint}" "${OUTPUT_DIR}/bios-redfish-system.json" || true
    redfish_get "${system_endpoint}/Bios" "${OUTPUT_DIR}/bios-redfish-bios.json" || true
    redfish_get "${system_endpoint}/Bios/Settings" "${OUTPUT_DIR}/bios-redfish-bios-settings.json" || true
    {
      printf 'system_endpoint=%s\n' "${system_endpoint}"
      printf 'bios_json=bios-redfish-bios.json\n'
      printf 'bios_settings_json=bios-redfish-bios-settings.json\n'
    } >> "${redfish_file}"
  else
    printf 'system_endpoint=not_discovered\n' >> "${redfish_file}"
  fi
}

collect_hardware() {
  local cpu_file="${OUTPUT_DIR}/hardware-cpu.txt"
  local mem_file="${OUTPUT_DIR}/hardware-memory.txt"
  local disk_file="${OUTPUT_DIR}/hardware-disk.txt"
  local nic_file="${OUTPUT_DIR}/hardware-nic.txt"

  run_capture "${cpu_file}" "uname-m" uname -m
  capture_if_available "${cpu_file}" "lscpu" lscpu
  capture_if_available "${cpu_file}" "lscpu-extended" lscpu -e
  capture_shell_if_available "${cpu_file}" "proc-cpuinfo-summary" awk "awk -F: '/^processor|^model name|^cpu cores|^siblings|^cache size|^cpu MHz/ {print}' /proc/cpuinfo | head -n 80"
  capture_shell_if_available "${cpu_file}" "cpu-cache-sysfs" find "find /sys/devices/system/cpu/cpu0/cache -maxdepth 2 -type f -name 'level' -o -name 'type' -o -name 'size' 2>/dev/null | sort | xargs -r -n1 sh -c 'printf \"%s=\" \"\$1\"; cat \"\$1\"' sh"
  capture_shell_if_available "${cpu_file}" "cpufreq-policy" find "find /sys/devices/system/cpu/cpufreq -maxdepth 2 -type f \\( -name scaling_governor -o -name scaling_cur_freq -o -name cpuinfo_max_freq -o -name cpuinfo_min_freq \\) 2>/dev/null | sort | xargs -r -n1 sh -c 'printf \"%s=\" \"\$1\"; cat \"\$1\"' sh"
  capture_if_available "${cpu_file}" "cpupower-frequency-info" cpupower frequency-info

  capture_if_available "${mem_file}" "dmidecode-memory" dmidecode -t memory
  capture_if_available "${mem_file}" "lshw-memory" lshw -class memory
  capture_if_available "${mem_file}" "free-human" free -h
  run_capture "${mem_file}" "proc-meminfo" cat /proc/meminfo

  capture_if_available "${disk_file}" "lsblk-detailed" lsblk -O
  capture_shell_if_available "${disk_file}" "lsblk-summary" lsblk "lsblk -d -o NAME,TYPE,SIZE,MODEL,SERIAL,VENDOR,ROTA,TRAN,PHY-SeC,LOG-SEC"
  capture_if_available "${disk_file}" "df-human" df -h
  capture_if_available "${disk_file}" "mount" mount
  capture_shell_if_available "${disk_file}" "block-queue" find "for d in /sys/block/*; do [ -d \"\$d/queue\" ] || continue; echo \"## \$d\"; for f in rotational scheduler nr_requests read_ahead_kb logical_block_size physical_block_size; do [ -r \"\$d/queue/\$f\" ] && printf '%s=%s\n' \"\$f\" \"\$(cat \"\$d/queue/\$f\")\"; done; done"
  capture_if_available "${disk_file}" "smartctl-scan" smartctl --scan

  capture_shell_if_available "${nic_file}" "lspci-network" lspci "lspci -nn | grep -Ei 'ethernet|network|infiniband'"
  capture_if_available "${nic_file}" "ip-link-brief" ip -br link
  capture_if_available "${nic_file}" "ip-address-brief" ip -br address
  capture_if_available "${nic_file}" "ifconfig-all" ifconfig -a
  capture_shell_if_available "${nic_file}" "ethtool-all-interfaces" ip "run_ethtool() { if command -v timeout >/dev/null 2>&1; then timeout 5 ethtool \"\$@\" 2>/dev/null || true; else ethtool \"\$@\" 2>/dev/null || true; fi; }; for iface in \$(ip -o link show | awk -F': ' '{print \$2}' | cut -d@ -f1); do echo \"## \${iface}\"; run_ethtool \"\${iface}\"; run_ethtool -i \"\${iface}\"; run_ethtool -k \"\${iface}\"; run_ethtool -g \"\${iface}\"; run_ethtool -c \"\${iface}\"; done"
}

collect_software() {
  local software_file="${OUTPUT_DIR}/software-versions.txt"
  local os_file="${OUTPUT_DIR}/os-config.txt"
  local kernel_file="${OUTPUT_DIR}/kernel-config.txt"
  local build_file="${OUTPUT_DIR}/build-system.txt"

  for item in \
    "gcc:gcc --version" \
    "g++:g++ --version" \
    "clang:clang --version" \
    "cmake:cmake --version" \
    "make:make --version" \
    "ninja:ninja --version" \
    "go:go version" \
    "rustc:rustc --version" \
    "cargo:cargo --version" \
    "python3:python3 --version" \
    "java:java -version" \
    "docker:docker --version" \
    "containerd:containerd --version" \
    "runc:runc --version" \
    "podman:podman --version" \
    "mysql:mysql --version" \
    "mysqld:mysqld --version" \
    "nginx:nginx -v" \
    "redis-server:redis-server --version"; do
    local command_name="${item%%:*}"
    local command_text="${item#*:}"
    capture_shell_if_available "${software_file}" "${command_name}-version" "${command_name}" "${command_text}"
  done

  capture_if_available "${build_file}" "cmake-version" cmake --version
  capture_if_available "${build_file}" "gcc-version" gcc --version
  capture_if_available "${build_file}" "go-version" go version
  capture_if_available "${build_file}" "docker-version" docker --version
  capture_if_available "${build_file}" "make-version" make --version
  capture_if_available "${build_file}" "ninja-version" ninja --version

  capture_if_available "${OUTPUT_DIR}/perf-diagnosis.txt" "perf-version" perf --version
  capture_shell_if_available "${OUTPUT_DIR}/perf-diagnosis.txt" "perf-event-paranoid" cat "cat /proc/sys/kernel/perf_event_paranoid"
  capture_shell_if_available "${OUTPUT_DIR}/perf-diagnosis.txt" "kptr-restrict" cat "cat /proc/sys/kernel/kptr_restrict"
  capture_shell_if_available "${OUTPUT_DIR}/perf-diagnosis.txt" "perf-list-hardware-events" perf "perf list 2>&1 | grep -E '(^|[[:space:]])(cycles|instructions|cache-misses|branches)([[:space:]]|$)' | head -n 40"
  capture_shell_if_available "${OUTPUT_DIR}/perf-diagnosis.txt" "perf-stat-smoke" perf "if command -v timeout >/dev/null 2>&1; then timeout 10 perf stat -e cycles,instructions -- true; else perf stat -e cycles,instructions -- true; fi"

  run_capture "${os_file}" "uname-all" uname -a
  run_capture "${os_file}" "proc-version" cat /proc/version
  capture_if_available "${os_file}" "sw-vers" sw_vers
  run_capture "${os_file}" "os-release" cat /etc/os-release
  capture_shell_if_available "${os_file}" "sysctl-all" sysctl "sysctl -a"
  capture_shell_if_available "${os_file}" "numa-balancing" cat "cat /proc/sys/kernel/numa_balancing"
  run_capture "${os_file}" "thp-enabled" cat /sys/kernel/mm/transparent_hugepage/enabled
  run_capture "${os_file}" "thp-defrag" cat /sys/kernel/mm/transparent_hugepage/defrag
  capture_shell_if_available "${os_file}" "hugepages" grep "grep -E 'Huge|AnonHuge' /proc/meminfo"
  capture_shell_if_available "${os_file}" "ulimit-all" sh "ulimit -a"
  capture_shell_if_available "${os_file}" "rps-xps" find "find /sys/class/net -path '*/queues/*/rps_cpus' -o -path '*/queues/*/xps_cpus' 2>/dev/null | sort | xargs -r -n1 sh -c 'printf \"%s=\" \"\$1\"; cat \"\$1\"' sh"
  capture_shell_if_available "${os_file}" "irq-affinity" find "find /proc/irq -maxdepth 2 -name smp_affinity_list 2>/dev/null | sort | head -n 200 | xargs -r -n1 sh -c 'printf \"%s=\" \"\$1\"; cat \"\$1\"' sh"
  capture_if_available "${os_file}" "systemctl-irqbalance" systemctl status irqbalance --no-pager
  capture_if_available "${os_file}" "tuned-adm-active" tuned-adm active

  run_capture "${kernel_file}" "uname-all" uname -a
  run_capture "${kernel_file}" "proc-cmdline" cat /proc/cmdline
  capture_shell_if_available "${kernel_file}" "kernel-config-proc-gz" zcat "zcat /proc/config.gz"
  capture_shell_if_available "${kernel_file}" "kernel-config-boot" sh "release=\$(uname -r); [ -r \"/boot/config-\${release}\" ] && cat \"/boot/config-\${release}\" || [ -r /boot/config ] && cat /boot/config || echo 'kernel config file not found'"
  capture_if_available "${kernel_file}" "lsmod" lsmod
}

copy_compatibility_files() {
  cp "${OUTPUT_DIR}/hardware-cpu.txt" "${OUTPUT_DIR}/cpu-info.txt"
  cp "${OUTPUT_DIR}/hardware-cpu.txt" "${OUTPUT_DIR}/numa-topology.txt"
  cp "${OUTPUT_DIR}/hardware-memory.txt" "${OUTPUT_DIR}/memory-info.txt"
  cp "${OUTPUT_DIR}/hardware-disk.txt" "${OUTPUT_DIR}/disk-info.txt"
  cp "${OUTPUT_DIR}/hardware-nic.txt" "${OUTPUT_DIR}/nic-info.txt"
  cp "${OUTPUT_DIR}/os-config.txt" "${OUTPUT_DIR}/os-kernel.txt"
  cp "${OUTPUT_DIR}/software-versions.txt" "${OUTPUT_DIR}/compiler-runtime.txt"
  cp "${OUTPUT_DIR}/os-config.txt" "${OUTPUT_DIR}/thp-status.txt"
  cp "${OUTPUT_DIR}/os-config.txt" "${OUTPUT_DIR}/hugepages-status.txt"
}

collect_runtime_context() {
  capture_if_available "${OUTPUT_DIR}/virtualization.txt" "systemd-detect-virt" systemd-detect-virt
  capture_shell_if_available "${OUTPUT_DIR}/virtualization.txt" "proc-cpuinfo-hypervisor" grep "grep -i hypervisor /proc/cpuinfo"
  capture_shell_if_available "${OUTPUT_DIR}/virtualization.txt" "proc-1-cgroup" cat "cat /proc/1/cgroup"
}

generate_summary_report() {
  local redfish_status="skipped"
  if [[ -n "${BMC_HOST}" && -n "${BMC_USER}" && -n "${BMC_PASS}" ]]; then
    redfish_status="attempted"
  fi

  REPORT_FILE="${REPORT_FILE}" OUTPUT_DIR="${OUTPUT_DIR}" REDFISH_STATUS="${redfish_status}" python3 - <<'PY'
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

out = Path(os.environ["OUTPUT_DIR"])
report = Path(os.environ["REPORT_FILE"])
redfish_status = os.environ.get("REDFISH_STATUS", "skipped")


def read(name):
    try:
        return (out / name).read_text(errors="ignore")
    except OSError:
        return ""


def read_json(name):
    try:
        return json.loads((out / name).read_text(errors="ignore"))
    except Exception:
        return None


def esc(value):
    return html.escape("" if value is None else str(value))


def redact(text):
    text = re.sub(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", "<mac-redacted>", text)
    text = re.sub(r"\binet\s+(\d{1,3}\.){3}\d{1,3}", "inet <ip-redacted>", text)
    text = re.sub(r"(?im)^(.*(?:password|passwd|pwd|secret|token).*)$", "<sensitive-line-redacted>", text)
    text = re.sub(r"(?im)^(.+Serial Number[^:]*:).*$", r"\1 <redacted>", text)
    text = re.sub(r"(?im)^(.+UUID[^:]*:).*$", r"\1 <redacted>", text)
    return text


def section_body(text, label):
    match = re.search(rf"===== {re.escape(label)} =====\n(?:time:[^\n]*\n)?(.*?)(?=\n===== |\Z)", text, re.S)
    return match.group(1).strip() if match else ""


def first_match(text, pattern, default="unknown", flags=re.M):
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else default


def kv(text, key, default="unknown"):
    return first_match(text, rf"^{re.escape(key)}:\s*(.+)$", default)


def file_link(name):
    return f'<a href="{esc(name)}">{esc(name)}</a>' if (out / name).exists() else esc(name)


def value_card(title, value, note=""):
    return f"<div class='metric'><span>{esc(title)}</span><strong>{esc(value)}</strong><em>{esc(note)}</em></div>"


def table(headers, rows):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


bios_info = read("bios-info.txt")
bios_json = read_json("bios-redfish-bios.json") or {}
bios_attrs = bios_json.get("Attributes") if isinstance(bios_json, dict) else {}
if not isinstance(bios_attrs, dict):
    bios_attrs = {}

cpu = read("hardware-cpu.txt")
mem = read("hardware-memory.txt")
disk = read("hardware-disk.txt")
nic = read("hardware-nic.txt")
soft = read("software-versions.txt")
osconf = read("os-config.txt")
kernel = read("kernel-config.txt")
perfdiag = read("perf-diagnosis.txt")
manifest = read("command-manifest.txt")

redfish_line = "已采集" if bios_attrs else ("已尝试，未取得 Attributes" if redfish_status == "attempted" else "未提供 BMC，已跳过")
board = first_match(bios_info, r"board_name=(.+)$")
bios_ver = first_match(bios_info, r"^([0-9][^\n]*)$", "unknown")

cpu_total = kv(cpu, "CPU(s)")
smt = kv(cpu, "Thread(s) per core")
cores_socket = kv(cpu, "Core(s) per socket")
sockets = kv(cpu, "Socket(s)")
numa_nodes = kv(cpu, "NUMA node(s)")
max_mhz = kv(cpu, "CPU max MHz")
min_mhz = kv(cpu, "CPU min MHz")
l1d = kv(cpu, "L1d cache")
l1i = kv(cpu, "L1i cache")
l2 = kv(cpu, "L2 cache")
l3 = kv(cpu, "L3 cache")
arch = kv(cpu, "Architecture")
vendor = kv(cpu, "Vendor ID")
model = kv(cpu, "Model name")

mem_total = first_match(mem, r"^Mem:\s+([^\n]+)$", "unknown")
mem_size = first_match(mem, r"size:\s*([^\n]+)", "unknown")
dimm_count = len(re.findall(r"Memory Device", mem))

lsblk_summary = section_body(disk, "lsblk-summary")
disk_rows = []
for line in lsblk_summary.splitlines()[1:12]:
    parts = line.split()
    if len(parts) < 3 or re.match(r"^(loop|nbd)", parts[0]):
        continue
    disk_model = parts[3] if len(parts) > 3 else ""
    serial = parts[4] if len(parts) > 4 else ""
    tran = "nvme" if "nvme" in parts else ("sata" if "sata" in parts else ("scsi" if "scsi" in parts else ""))
    rota = ""
    if tran and tran in parts:
        idx = parts.index(tran)
        rota = parts[idx - 1] if idx > 0 else ""
    elif len(parts) > 6:
        rota = parts[6]
    disk_rows.append([esc(redact(x)) for x in [parts[0], parts[1], parts[2], disk_model, serial, rota, tran]])
if not disk_rows:
    disk_rows = [["详见 hardware-disk.txt", "", "", "", "", "", ""]]

nic_devices = section_body(nic, "lspci-network")
nic_rows = [[esc(redact(line.strip()))] for line in nic_devices.splitlines()[:12] if line.strip()]
if not nic_rows:
    nic_rows = [["详见 hardware-nic.txt"]]

software_names = ["gcc", "g++", "clang", "cmake", "make", "ninja", "go", "python3", "java", "docker", "containerd", "mysql", "mysqld", "nginx", "redis-server"]
soft_rows = []
for name in software_names:
    body = section_body(soft, f"{name}-version")
    status = "missing"
    value = "未发现"
    if body:
        status = "ok" if "command not found" not in body and "command failed" not in body else "failed"
        value = redact(" ".join(body.splitlines()[:2]))[:180]
    soft_rows.append([esc(name), f"<span class='pill {status}'>{esc(status)}</span>", esc(value)])

os_name = first_match(osconf, r'^PRETTY_NAME="?([^"\n]+)', "unknown")
kernel_uname = section_body(osconf, "uname-all").splitlines()[0] if section_body(osconf, "uname-all") else "unknown"
numa_bal = section_body(osconf, "numa-balancing").splitlines()[0] if section_body(osconf, "numa-balancing") else "unknown"
thp_enabled = section_body(osconf, "thp-enabled").splitlines()[0] if section_body(osconf, "thp-enabled") else "unknown"
thp_defrag = section_body(osconf, "thp-defrag").splitlines()[0] if section_body(osconf, "thp-defrag") else "unknown"
tuned = section_body(osconf, "tuned-adm-active") or "unknown"
cmdline = section_body(kernel, "proc-cmdline") or "unknown"

perf_keywords = re.compile(r"perf|power|turbo|boost|cstate|c-state|pstate|p-state|numa|interleav|smt|thread|freq|frequency|energy|hpc|virtual|iommu|smmu|pcie|ras|ecc", re.I)
bios_focus = []
for key, val in sorted(bios_attrs.items()):
    if perf_keywords.search(f"{key} {val}"):
        bios_focus.append([f"<code>{esc(key)}</code>", esc(val)])
bios_focus = bios_focus[:16] or [["Redfish BIOS Attributes", esc(redfish_line)]]

fail_count = len(re.findall(r"\] FAIL ", manifest))
skip_count = len(re.findall(r"\] SKIP ", manifest))
ok_count = len(re.findall(r"\] OK\s+", manifest))


def status_badge(status):
    return f"<span class='diag {esc(status)}'>{esc(status)}</span>"


def contains_any(text, words):
    low = text.lower()
    return [word for word in words if word.lower() in low]


bios_text = " ".join(f"{k}={v}" for k, v in bios_attrs.items()) + " " + bios_info
bios_perf_hits = contains_any(bios_text, ["Performance", "Maximum Performance", "HPC", "High Performance"])
bios_powersave_hits = contains_any(bios_text, ["Power Efficiency", "powersave", "power save", "balanced", "energy efficient", "low power"])
if bios_attrs:
    if bios_perf_hits and not bios_powersave_hits:
        bios_diag_status = "passed"
        bios_diag_finding = "Redfish BIOS 属性中存在性能取向配置。"
    elif bios_powersave_hits:
        bios_diag_status = "degraded"
        bios_diag_finding = "BIOS 中存在节能/均衡相关可选项或当前值，需要人工确认实际 profile。"
    else:
        bios_diag_status = "degraded"
        bios_diag_finding = "已采集 Redfish BIOS，但未识别到明确性能 profile。"
elif bios_info.strip():
    bios_diag_status = "degraded"
    bios_diag_finding = "仅有 OS 侧 BIOS/DMI 证据，缺少 Redfish 当前配置。"
else:
    bios_diag_status = "unknown"
    bios_diag_finding = "未采集到 BIOS 证据。"

perf_version = section_body(perfdiag, "perf-version")
perf_list = section_body(perfdiag, "perf-list-hardware-events")
perf_smoke = section_body(perfdiag, "perf-stat-smoke")
perf_paranoid = section_body(perfdiag, "perf-event-paranoid").splitlines()[0] if section_body(perfdiag, "perf-event-paranoid") else "unknown"
kptr_restrict = section_body(perfdiag, "kptr-restrict").splitlines()[0] if section_body(perfdiag, "kptr-restrict") else "unknown"
if not perf_version or "command not found" in perf_version:
    perf_diag_status = "failed"
    perf_diag_finding = "目标机未发现 perf 命令，后续 perf/topdown/火焰图能力受阻。"
elif "not supported" in perf_smoke.lower() or "permission" in perf_smoke.lower() or "No permission" in perf_smoke:
    perf_diag_status = "degraded"
    perf_diag_finding = "perf 存在但 smoke test 权限或事件受限。"
elif perf_list and "cycles" in perf_list and "instructions" in perf_smoke:
    perf_diag_status = "passed"
    perf_diag_finding = "perf 命令、硬件事件列表和最小 perf stat smoke test 可用。"
else:
    perf_diag_status = "degraded"
    perf_diag_finding = "perf 存在，但硬件事件或 smoke test 证据不完整。"

kernel_patch_status = "skipped"
kernel_patch_finding = "未提供 kernel patch manifest，无法判断目标场景补丁齐全性。"
reference_status = "skipped"
reference_finding = "未提供历史 reference 问题集，本轮不做历史问题回归检查。"

os_diag = []
os_diag.append(["NUMA balancing", esc(numa_bal), "passed" if numa_bal == "0" else "degraded", "建议性能场景关闭自动 NUMA balancing。"])
os_diag.append(["THP enabled", esc(thp_enabled), "passed" if "[never]" in thp_enabled else "degraded", "数据库/低延迟场景通常需确认 THP 策略。"])
os_diag.append(["THP defrag", esc(thp_defrag), "passed" if "[never]" in thp_defrag or "[madvise]" in thp_defrag else "degraded", "THP defrag 可能引入延迟抖动。"])
tuned_status = "passed" if "Current active profile" in tuned else "degraded"
os_diag.append(["tuned profile", esc("active" if "Current active profile" in tuned else tuned[:80]), tuned_status, "建议确认 tuned 是否为性能 profile。"])

diagnosis_rows = [
    ["BIOS 高性能配置", status_badge(bios_diag_status), esc(bios_diag_finding), f"{file_link('bios-redfish-bios.json')} {file_link('bios-info.txt')}"],
    ["Perf / PMU 采集能力", status_badge(perf_diag_status), esc(perf_diag_finding), f"perf_event_paranoid={esc(perf_paranoid)}, kptr_restrict={esc(kptr_restrict)}；{file_link('perf-diagnosis.txt')}"],
    ["内核补丁齐全性", status_badge(kernel_patch_status), esc(kernel_patch_finding), file_link('kernel-config.txt')],
    ["历史问题集回归", status_badge(reference_status), esc(reference_finding), "未提供 reference issue set"],
]
os_diag_rows = [[esc(name), status_badge(status), value, esc(note)] for name, value, status, note in os_diag]

raw_files = [
    ("BIOS 配置", ["bios-info.txt", "bios-redfish.txt", "bios-redfish-bios.json", "bios-redfish-bios-settings.json"]),
    ("硬件配置", ["hardware-cpu.txt", "hardware-memory.txt", "hardware-disk.txt", "hardware-nic.txt"]),
    ("软件配置", ["software-versions.txt", "build-system.txt", "os-config.txt", "kernel-config.txt"]),
    ("运行环境", ["environment-type.txt", "container-limits.txt", "virtualization.txt"]),
    ("审计", ["command-manifest.txt", "optimization-timeline.txt", "README.txt"]),
]
raw_html = ""
for group, files in raw_files:
    links = "".join(f"<li>{file_link(name)}</li>" for name in files if (out / name).exists())
    raw_html += f"<div class='raw-card'><h3>{esc(group)}</h3><ul>{links}</ul></div>"

css = """
:root{--ink:#172033;--muted:#64748b;--bg:#f5f7fb;--panel:#fff;--line:#d8e1ec;--head:#142033;--green:#e8f7ef;--amber:#fff7df;--red:#fff1f2;--blue:#e8f1ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.55}header{background:var(--head);color:#fff;padding:32px 24px}.wrap{max-width:1280px;margin:0 auto}.lead{color:#dce7f7;max-width:960px}main{padding:24px}section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:22px;margin-bottom:18px;box-shadow:0 10px 24px rgba(15,23,42,.06)}h2{margin:0 0 14px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metric{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px}.metric span{display:block;color:var(--muted);font-size:13px}.metric strong{display:block;font-size:24px;line-height:1.25;margin:4px 0}.metric em{display:block;color:var(--muted);font-style:normal;font-size:12px}.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}th{background:#eef3f9;color:#233047}code{background:#eef2f7;padding:1px 5px;border-radius:5px}.pill,.diag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 8px;background:#fff;font-size:12px}.ok,.passed{background:var(--green)}.missing,.skipped,.unknown{background:#f1f5f9}.failed{background:var(--red)}.degraded{background:var(--amber)}.note{color:var(--muted);font-size:13px}.raw-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.raw-card{border:1px solid var(--line);border-radius:8px;padding:12px;background:#fbfdff}.raw-card h3{margin:0 0 8px;font-size:15px}.raw-card ul{margin:0;padding-left:18px}.kv{display:grid;grid-template-columns:180px 1fr;border:1px solid var(--line);border-bottom:0}.kv div{padding:8px 10px;border-bottom:1px solid var(--line)}.kv div:nth-child(odd){background:#f8fafc;color:var(--muted);font-weight:700}@media(max-width:980px){.metrics,.two,.three,.raw-grid{grid-template-columns:1fr}table{display:block;overflow-x:auto;white-space:nowrap}.kv{grid-template-columns:1fr}}
"""

html_doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>环境信息备份报告</title><style>{css}</style></head><body>
<header><div class="wrap"><h1>环境信息备份报告</h1><p class="lead">按 BIOS 配置、硬件配置信息、软件配置三类汇总。HTML 为精简概览，完整原始输出保留在同目录文件中供详细查阅。生成时间：{esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p></div></header>
<main class="wrap">
<section><h2>采集概览</h2><div class="metrics">{value_card('Redfish BIOS', redfish_line, 'BMC 凭据不写入报告')}{value_card('命令成功', ok_count, '详见 command-manifest.txt')}{value_card('命令失败', fail_count, '失败不阻断采集')}{value_card('命令跳过', skip_count, '通常为工具不存在')}</div></section>
<section><h2>1. BIOS 配置</h2><div class="two"><div><div class="kv"><div>BIOS Version</div><div>{esc(bios_ver)}</div><div>Board</div><div>{esc(board)}</div><div>Redfish 状态</div><div>{esc(redfish_line)}</div><div>BIOS Attributes</div><div>{esc(len(bios_attrs))}</div></div></div><div>{table(['重点属性','当前值'], bios_focus)}</div></div><p class="note">更多 BIOS 属性见 {file_link('bios-redfish-bios.json')}；OS 侧固件信息见 {file_link('bios-info.txt')}。</p></section>
<section><h2>2. 硬件配置信息</h2><div class="metrics">{value_card('CPU 总数', cpu_total, f'{arch} / {vendor}')}{value_card('SMT', smt, f'Core/Socket {cores_socket}, Socket {sockets}')}{value_card('NUMA', numa_nodes, f'MHz {min_mhz}-{max_mhz}')}{value_card('内存', mem_size if mem_size!='unknown' else mem_total, f'DIMM entries {dimm_count}')}</div><div class="three"><div><h3>CPU / Cache</h3><div class="kv"><div>Model</div><div>{esc(model)}</div><div>L1d</div><div>{esc(l1d)}</div><div>L1i</div><div>{esc(l1i)}</div><div>L2</div><div>{esc(l2)}</div><div>L3</div><div>{esc(l3)}</div></div></div><div><h3>磁盘</h3>{table(['NAME','TYPE','SIZE','MODEL','SERIAL','ROTA','TRAN'], disk_rows[:8])}</div><div><h3>网卡</h3>{table(['PCIe 网络设备'], nic_rows[:8])}</div></div><p class="note">硬件原始文件：{file_link('hardware-cpu.txt')}、{file_link('hardware-memory.txt')}、{file_link('hardware-disk.txt')}、{file_link('hardware-nic.txt')}。</p></section>
<section><h2>3. 软件配置</h2><div class="metrics">{value_card('OS', os_name, '发行版')}{value_card('NUMA balancing', numa_bal, 'kernel.numa_balancing')}{value_card('THP', thp_enabled, 'enabled')}{value_card('Tuned', 'active' if 'Current active profile' in tuned else tuned[:32], '性能 profile')}</div><div class="two"><div><h3>软件版本 / 构建链</h3>{table(['软件','状态','输出摘要'], soft_rows)}</div><div><h3>OS / 内核关键项</h3><div class="kv"><div>Kernel</div><div>{esc(kernel_uname)}</div><div>THP defrag</div><div>{esc(thp_defrag)}</div><div>Cmdline</div><div>{esc(cmdline[:260])}</div><div>环境类型</div><div>{esc(read('environment-type.txt').strip())}</div><div>容器限制</div><div>{esc(read('container-limits.txt').strip())}</div></div></div></div><p class="note">软件与内核原始文件：{file_link('software-versions.txt')}、{file_link('os-config.txt')}、{file_link('kernel-config.txt')}。</p></section>
<section><h2>4. 环境诊断</h2><p class="note">诊断基于本次目标机采集文件生成，不依赖本机状态。</p>{table(['诊断项','状态','结论','证据'], diagnosis_rows)}<h3>OS 关键参数诊断</h3>{table(['参数','状态','当前值','说明'], os_diag_rows)}</section>
<section><h2>原始文件索引</h2><p class="note">HTML 只保留精简摘要；所有命令原始 stdout/stderr 均保留在以下文件中。</p><div class="raw-grid">{raw_html}</div></section>
</main></body></html>"""

report.write_text(html_doc, encoding="utf-8")
print(f"Generated HTML report at {report}")
PY
}

prompt_bmc_if_interactive

log_manifest "START environment backup output_dir=${OUTPUT_DIR}"
printf 'backup_start=%s\n' "$(timestamp)" >> "${TIMELINE_FILE}"

detect_environment
collect_bios
collect_hardware
collect_software
collect_runtime_context
copy_compatibility_files

{
  printf 'backup_end=%s\n' "$(timestamp)"
  printf 'manifest=%s\n' "${MANIFEST_FILE}"
  printf 'report=%s\n' "${REPORT_FILE}"
} >> "${TIMELINE_FILE}"

generate_summary_report

log_manifest "END environment backup output_dir=${OUTPUT_DIR}"
echo "Collected environment backup at ${OUTPUT_DIR}"
echo "Generated summary report at ${REPORT_FILE}"
