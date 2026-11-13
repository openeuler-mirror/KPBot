#!/usr/bin/env bash
# Deploy CubeSandbox from an official, architecture-matching online release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISCOVER_RELEASE="${SCRIPT_DIR}/discover_release.py"
PARSE_ENV="${SCRIPT_DIR}/parse_env.py"
LOG_PREFIX="[cubesandbox-deployment]"
INSTALL_ROOT="/usr/local/services/cubetoolbox"
readonly SCRIPT_DIR DISCOVER_RELEASE PARSE_ENV LOG_PREFIX INSTALL_ROOT
ENV_FILE=""
PREFLIGHT_ONLY=0
PREFLIGHT_FAILURES=0
NODE_IP_EXPLICIT=0
WORK_DIR=""
PARSED_ENV_FILE=""

cleanup() {
  if [[ -n "${PARSED_ENV_FILE}" ]]; then
    rm -f "${PARSED_ENV_FILE}"
  fi
  if [[ -n "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}
trap cleanup EXIT

log() {
  printf '%s %s\n' "${LOG_PREFIX}" "$*"
}

warn() {
  printf '%s WARNING: %s\n' "${LOG_PREFIX}" "$*" >&2
}

die() {
  printf '%s ERROR: %s\n' "${LOG_PREFIX}" "$*" >&2
  exit 1
}

preflight_fail() {
  printf '%s ERROR: %s\n' "${LOG_PREFIX}" "$*" >&2
  PREFLIGHT_FAILURES=$((PREFLIGHT_FAILURES + 1))
}

usage() {
  cat <<'EOF'
Usage: bash deploy.sh [options]

Deploy a single-node, all-in-one CubeSandbox control+compute host from the
official online release. No template, sandbox workload, or benchmark is run.

Options:
  --env <file>       Source deployment overrides from a shell environment file.
  --preflight-only   Read-only host checks and release discovery; do not install.
  -h, --help         Show this help.

Common environment overrides:
  CUBE_VERSION=latest|vX.Y.Z       Release to install (default: latest).
  MIRROR=cn|""                    CN release/images, or empty for GitHub/global.
  CUBE_SANDBOX_NODE_IP=<IPv4>     Required when multiple/no host IPs are detected.
  CUBE_PVM_ENABLE=auto|0|1        Guest kernel mode (default: auto).
  CUBE_SANDBOX_NETWORK_CIDR=<CIDR> Unused private sandbox CIDR, if needed.
  CUBE_SANDBOX_DOWNLOAD_URL=<URL> Explicit architecture-matching bundle URL.
  CUBE_SANDBOX_SHA256=<hex>        Expected digest for an explicit bundle URL.

Only fresh installation is supported. Existing, partial, or unrecognized install
roots are refused; use the official installer manually for upgrade/recovery.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      [[ $# -ge 2 ]] || die "--env requires a file path"
      ENV_FILE="$2"
      shift 2
      ;;
    --preflight-only)
      PREFLIGHT_ONLY=1
      shift
      ;;
    --upgrade)
      die "upgrade is outside this skill's scope; use the official installer manually after backup and user confirmation"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
done

for forbidden_switch in ONE_CLICK_SKIP_PRECHECK ONE_CLICK_SKIP_PVM_CHECK; do
  if [[ "${!forbidden_switch:-0}" != "0" ]]; then
    die "${forbidden_switch} is forbidden; preflight and PVM consistency checks cannot be bypassed"
  fi
done
if [[ "${ONE_CLICK_RUN_QUICKCHECK:-1}" != "1" ]]; then
  die "ONE_CLICK_RUN_QUICKCHECK=0 is forbidden; service health verification is mandatory"
fi
if [[ "${ONE_CLICK_MODE:-install}" != "install" ]]; then
  die "ONE_CLICK_MODE is managed by deploy.sh; only fresh install is supported"
fi
[[ -z "${ONE_CLICK_PACKAGE_TAR:-}" ]] \
  || die "ONE_CLICK_PACKAGE_TAR is forbidden; the verified official bundle must be used"
unset ONE_CLICK_SKIP_PRECHECK ONE_CLICK_SKIP_PVM_CHECK ONE_CLICK_PACKAGE_TAR

if [[ -n "${ENV_FILE}" ]]; then
  [[ -f "${ENV_FILE}" && -r "${ENV_FILE}" ]] || die "environment file is not readable: ${ENV_FILE}"
  [[ -f "${PARSE_ENV}" ]] || die "missing environment parser: ${PARSE_ENV}"
  ENV_FILE="$(readlink -f "${ENV_FILE}")"
  log "strictly parsing environment overrides from ${ENV_FILE} (no shell evaluation)"
  PARSED_ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/cubesandbox-env.XXXXXX")"
  chmod 600 "${PARSED_ENV_FILE}"
  if ! python3 "${PARSE_ENV}" "${ENV_FILE}" >"${PARSED_ENV_FILE}"; then
    die "environment file validation failed"
  fi
  while IFS= read -r -d '' PARSED_KEY; do
    if ! IFS= read -r -d '' PARSED_VALUE; then
      die "environment parser returned an incomplete record"
    fi
    export "${PARSED_KEY}=${PARSED_VALUE}"
  done <"${PARSED_ENV_FILE}"
fi

CUBE_VERSION="${CUBE_VERSION:-latest}"
MIRROR="${MIRROR-cn}"
CUBE_PVM_ENABLE="${CUBE_PVM_ENABLE:-auto}"
ONE_CLICK_DEPLOY_ROLE="${ONE_CLICK_DEPLOY_ROLE:-control}"
[[ "${CUBE_SANDBOX_INSTALL_ROOT:-${INSTALL_ROOT}}" == "${INSTALL_ROOT}" ]] \
  || die "custom CUBE_SANDBOX_INSTALL_ROOT is unsupported; the official installer uses ${INSTALL_ROOT}"
CUBE_SANDBOX_INSTALL_ROOT="${INSTALL_ROOT}"
[[ -n "${CUBE_SANDBOX_NODE_IP:-}" ]] && NODE_IP_EXPLICIT=1

case "${MIRROR}" in
  ""|cn) ;;
  *) die "MIRROR must be 'cn' or empty (got '${MIRROR}')" ;;
esac
case "${CUBE_PVM_ENABLE}" in
  auto|0|1) ;;
  *) die "CUBE_PVM_ENABLE must be auto, 0, or 1" ;;
esac
[[ "${ONE_CLICK_DEPLOY_ROLE}" == "control" ]] \
  || die "this skill supports only the single-node all-in-one control role"
[[ -f "${DISCOVER_RELEASE}" ]] || die "missing release helper: ${DISCOVER_RELEASE}"

case "$(uname -m)" in
  x86_64|amd64)
    RELEASE_ARCH="amd64"
    HOST_ARCH="x86_64"
    ;;
  aarch64|arm64)
    RELEASE_ARCH="arm64"
    HOST_ARCH="aarch64"
    ;;
  *)
    die "unsupported host architecture: $(uname -m); expected x86_64 or aarch64"
    ;;
esac

pvm_module_present() {
  [[ -d /sys/module/kvm_pvm ]] || grep -qE '^kvm_pvm[[:space:]]' /proc/modules 2>/dev/null
}

pvm_kernel_present() {
  [[ "$(uname -r)" == *cubesandbox.pvm* ]]
}

if [[ "${CUBE_PVM_ENABLE}" == "auto" ]]; then
  if [[ "${HOST_ARCH}" == "x86_64" ]] && { pvm_module_present || pvm_kernel_present; }; then
    CUBE_PVM_ENABLE=1
  else
    CUBE_PVM_ENABLE=0
  fi
fi
export CUBE_PVM_ENABLE

detect_default_node_ip() {
  local default_dev=""
  default_dev="$(ip -4 route show default 2>/dev/null | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") print $(i + 1) }' || true)"
  [[ -n "${default_dev}" ]] || return 1
  ip -4 -o addr show dev "${default_dev}" scope global 2>/dev/null \
    | awk 'NR == 1 { split($4, address, "/"); print address[1] }'
}

if [[ -z "${CUBE_SANDBOX_NODE_IP:-}" ]] && command -v ip >/dev/null 2>&1; then
  CUBE_SANDBOX_NODE_IP="$(detect_default_node_ip || true)"
  if [[ -n "${CUBE_SANDBOX_NODE_IP}" ]]; then
    export CUBE_SANDBOX_NODE_IP
    log "auto-detected CUBE_SANDBOX_NODE_IP=${CUBE_SANDBOX_NODE_IP}"
  fi
fi

install_root_state() {
  if [[ -f "${INSTALL_ROOT}/.one-click.env" ]]; then
    printf 'installed\n'
    return 0
  fi
  if [[ -d "${INSTALL_ROOT}" ]]; then
    local marker
    for marker in CubeMaster CubeAPI Cubelet CubeOps CubeTemplateCenter cubeproxy scripts env.example; do
      if [[ -e "${INSTALL_ROOT}/${marker}" ]]; then
        printf 'partial\n'
        return 0
      fi
    done
    if [[ -n "$(find "${INSTALL_ROOT}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
      printf 'unknown\n'
      return 0
    fi
  elif [[ -e "${INSTALL_ROOT}" ]]; then
    printf 'unknown\n'
    return 0
  fi
  printf 'absent\n'
}

install_root_error() {
  local context="$1" message="$2"
  if [[ "${context}" == "preflight" ]]; then
    preflight_fail "${message}"
  else
    die "${message}"
  fi
}

validate_install_root() {
  local context="$1" state
  state="$(install_root_state)"
  case "${state}" in
    installed)
      if [[ "${context}" == "preflight" ]] && (( PREFLIGHT_ONLY == 1 )); then
        warn "existing installation detected at ${INSTALL_ROOT}; fresh installation would be refused"
      else
        install_root_error "${context}" "CubeSandbox already exists at ${INSTALL_ROOT}; this skill is fresh-install only, so use the official installer manually for upgrade/recovery"
      fi
      ;;
    absent)
      ;;
    partial|unknown)
      install_root_error "${context}" "partial or unrecognized content exists at ${INSTALL_ROOT}; stop and ask the user for manual backup/recovery (neither fresh install nor --upgrade is safe)"
      ;;
    *)
      install_root_error "${context}" "could not determine installation state at ${INSTALL_ROOT}"
      ;;
  esac
}

check_sandbox_cidr() {
  local cidr="${CUBE_SANDBOX_NETWORK_CIDR:-192.168.0.0/18}" error=""
  if ! error="$(python3 - "${cidr}" 2>&1 <<'PY'
import ipaddress
import subprocess
import sys

try:
    sandbox = ipaddress.ip_network(sys.argv[1], strict=True)
    if sandbox.version != 4 or not sandbox.is_private or not 16 <= sandbox.prefixlen <= 24:
        raise ValueError("must be a private IPv4 network with a /16 through /24 mask")

    def check_candidate(candidate, source):
        if "." not in candidate or candidate in ("0.0.0.0", "0.0.0.0/0"):
            return
        try:
            host_network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            return
        if sandbox.overlaps(host_network):
            raise ValueError("overlaps " + source + " " + str(host_network))

    addresses = subprocess.check_output(
        ["ip", "-4", "-o", "addr", "show"], text=True, stderr=subprocess.STDOUT)
    for line in addresses.splitlines():
        for token in line.split():
            if "/" in token:
                check_candidate(token, "host interface")

    routes = subprocess.check_output(
        ["ip", "-4", "route", "show", "table", "all"], text=True, stderr=subprocess.STDOUT)
    for line in routes.splitlines():
        for token in line.split():
            if token != "default":
                check_candidate(token, "host route")

    with open("/etc/resolv.conf", encoding="utf-8") as resolv:
        for line in resolv:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                check_candidate(fields[1], "DNS nameserver")
except (ValueError, OSError, subprocess.SubprocessError) as exc:
    print("invalid CUBE_SANDBOX_NETWORK_CIDR " + sys.argv[1] + ": " + str(exc), file=sys.stderr)
    raise SystemExit(1)
PY
  )"; then
    preflight_fail "${error}"
  fi
}

bind_port() {
  local bind="$1"
  printf '%s\n' "${bind##*:}"
}

API_PORT="$(bind_port "${CUBE_API_BIND:-0.0.0.0:3000}")"
OPS_PORT="$(bind_port "${CUBE_OPS_BIND:-0.0.0.0:3010}")"
# Keep health probes and WebUI routing aligned with custom listen endpoints.
# Explicit companion settings win; otherwise derive them from the selected bind.
CUBE_API_HEALTH_ADDR="${CUBE_API_HEALTH_ADDR:-127.0.0.1:${API_PORT}}"
CUBE_OPS_HEALTH_ADDR="${CUBE_OPS_HEALTH_ADDR:-127.0.0.1:${OPS_PORT}}"
CUBE_OPS_UPSTREAM="${CUBE_OPS_UPSTREAM:-http://host.docker.internal:${OPS_PORT}}"
WEB_UI_UPSTREAM="${WEB_UI_UPSTREAM:-http://host.docker.internal:${OPS_PORT}}"
export CUBE_API_HEALTH_ADDR CUBE_OPS_HEALTH_ADDR CUBE_OPS_UPSTREAM WEB_UI_UPSTREAM

check_listening_ports() {
  local ports=("${API_PORT}" 8089 "${OPS_PORT}" 9999 "${CUBE_EGRESS_ADMIN_PORT:-9091}")
  if [[ "${CUBE_PROXY_ENABLE:-1}" == "1" ]]; then
    ports+=("${CUBE_PROXY_HTTP_PORT:-80}" "${CUBE_PROXY_HTTPS_PORT:-443}" \
      "${CUBE_PROXY_GRPC_PORT:-9090}" "${CUBE_PROXY_ADMIN_PORT:-8082}")
  fi
  if [[ "${WEB_UI_ENABLE:-1}" == "1" ]]; then
    ports+=("${WEB_UI_HOST_PORT:-12088}")
  fi
  if [[ -z "${CUBE_EXTERNAL_MYSQL_HOST:-}" && -z "${CUBE_EXTERNAL_POSTGRES_HOST:-}" ]]; then
    ports+=("${CUBE_SANDBOX_MYSQL_PORT:-3306}")
  fi
  if [[ -z "${CUBE_EXTERNAL_REDIS_HOST:-}" && -z "${CUBE_EXTERNAL_REDIS_SENTINEL_NODES:-}" ]]; then
    ports+=("${CUBE_SANDBOX_REDIS_PORT:-6379}")
  fi
  if [[ "${CUBE_SANDBOX_MINIO_ENABLED:-1}" == "1" ]]; then
    ports+=("${CUBE_SANDBOX_MINIO_API_PORT:-9000}" "${CUBE_SANDBOX_MINIO_CONSOLE_PORT:-9001}")
  fi

  local port
  local -A checked=()
  for port in "${ports[@]}"; do
    if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
      preflight_fail "invalid TCP port configured: ${port}"
      continue
    fi
    [[ -z "${checked[${port}]:-}" ]] || continue
    checked[${port}]=1
    local listeners=""
    if ! listeners="$(ss -H -ltn "sport = :${port}" 2>&1)"; then
      preflight_fail "failed to probe TCP port ${port}: ${listeners}"
    elif [[ -n "${listeners}" ]]; then
      preflight_fail "TCP port ${port} is already listening; confirm its owner or configure an official port override"
    fi
  done
}

run_host_preflight() {
  log "running read-only host preflight for ${HOST_ARCH}"

  [[ "$(uname)" == "Linux" ]] || preflight_fail "CubeSandbox supports Linux only"
  [[ "${EUID}" -eq 0 ]] || preflight_fail "run as root; systemd, Docker, DNS, and network changes are required"
  [[ -d /run/systemd/system ]] || preflight_fail "systemd is not running (/run/systemd/system is absent)"
  [[ -x /usr/bin/systemctl || -x /bin/systemctl ]] || preflight_fail "systemctl is required"

  local command_name
  for command_name in bash curl python3 tar awk grep stat df sha256sum ip ss flock find; do
    command -v "${command_name}" >/dev/null 2>&1 \
      || preflight_fail "required command is missing: ${command_name}"
  done

  if ! command -v docker >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1 \
      && ! command -v yum >/dev/null 2>&1 \
      && ! command -v dnf >/dev/null 2>&1; then
      preflight_fail "Docker is absent and apt-get/yum/dnf is unavailable for the official installer"
    fi
  else
    local docker_path docker_resolved
    docker_path="$(command -v docker)"
    docker_resolved="$(readlink -f "${docker_path}" 2>/dev/null || true)"
    if [[ "${docker_path}" == /snap/* || "${docker_resolved}" == /snap/* || "${docker_resolved}" == /usr/bin/snap ]]; then
      preflight_fail "snap Docker is unsupported because it cannot read /usr/local/services"
    fi
  fi

  local glibc_version glibc_major glibc_minor
  glibc_version="$(ldd --version 2>&1 | awk 'NR == 1 { print $NF }' || true)"
  glibc_major="${glibc_version%%.*}"
  glibc_minor="${glibc_version#*.}"
  glibc_minor="${glibc_minor%%.*}"
  if [[ ! "${glibc_major}" =~ ^[0-9]+$ || ! "${glibc_minor}" =~ ^[0-9]+$ ]] \
    || (( glibc_major < 2 || (glibc_major == 2 && glibc_minor < 31) )); then
    preflight_fail "glibc >= 2.31 is required (detected: ${glibc_version:-unknown})"
  else
    log "glibc ${glibc_version}: OK"
  fi

  local memory_kb cpu_count
  memory_kb="$(awk '/MemTotal/ { print $2 }' /proc/meminfo 2>/dev/null || true)"
  if [[ ! "${memory_kb}" =~ ^[0-9]+$ ]] || (( memory_kb < 7500000 )); then
    preflight_fail "at least 8 GB memory is required"
  fi
  cpu_count="$(nproc 2>/dev/null || true)"
  if [[ ! "${cpu_count}" =~ ^[0-9]+$ ]] || (( cpu_count < 4 )); then
    preflight_fail "at least 4 logical CPUs are required (detected: ${cpu_count:-unknown})"
  fi

  local check_path="/data/cubelet" fs_type available_kb
  while [[ ! -e "${check_path}" ]]; do
    [[ "${check_path}" != "/" ]] || break
    check_path="$(dirname "${check_path}")"
  done
  [[ -w "${check_path}" ]] || preflight_fail "${check_path} is not writable"
  fs_type="$(stat -fc %T "${check_path}" 2>/dev/null || true)"
  if [[ "${fs_type}" != "xfs" ]]; then
    preflight_fail "/data/cubelet must be on XFS (nearest existing path ${check_path}: ${fs_type:-unknown}); no disk is formatted automatically"
  fi
  available_kb="$(df -Pk "${check_path}" 2>/dev/null | awk 'END { print $4 }' || true)"
  if [[ ! "${available_kb}" =~ ^[0-9]+$ ]] || (( available_kb < 51200000 )); then
    preflight_fail "/data/cubelet needs at least 50 GiB free space"
  fi

  if ! grep -qw bpf /proc/filesystems 2>/dev/null; then
    preflight_fail "the kernel does not support bpffs/eBPF (CONFIG_BPF_SYSCALL)"
  elif [[ "$(stat -fc %T /sys/fs/bpf 2>/dev/null || true)" != "bpf" ]]; then
    log "AUTO: /sys/fs/bpf will be mounted as bpffs during deployment"
  fi

  if [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null || true)" == "cgroup2fs" ]]; then
    grep -qw cpu /sys/fs/cgroup/cgroup.controllers 2>/dev/null \
      || preflight_fail "cgroup v2 does not expose the cpu controller"
  fi

  if [[ "${HOST_ARCH}" == "aarch64" && "${CUBE_PVM_ENABLE}" == "1" ]]; then
    preflight_fail "PVM host kernels are x86_64-only; ARM64 requires native KVM and CUBE_PVM_ENABLE=0"
  fi
  if pvm_module_present && [[ "${CUBE_PVM_ENABLE}" != "1" ]]; then
    preflight_fail "kvm_pvm is loaded but CUBE_PVM_ENABLE=${CUBE_PVM_ENABLE}; use auto or 1"
  fi
  if [[ "${CUBE_PVM_ENABLE}" == "1" ]] && ! pvm_module_present; then
    if [[ "${HOST_ARCH}" == "x86_64" ]] && pvm_kernel_present; then
      log "AUTO: the PVM kernel is active; kvm_pvm will be loaded during deployment"
    else
      preflight_fail "CUBE_PVM_ENABLE=1 requires an active PVM host kernel; install it and reboot before deployment"
    fi
  fi
  if [[ ! -e /dev/kvm ]]; then
    if [[ "${CUBE_PVM_ENABLE}" == "1" ]] && pvm_kernel_present && ! pvm_module_present; then
      log "AUTO: /dev/kvm should appear after loading kvm_pvm"
    elif [[ "${HOST_ARCH}" == "aarch64" ]]; then
      preflight_fail "/dev/kvm is absent; ARM64 needs native KVM (PVM is not supported)"
    else
      preflight_fail "/dev/kvm is absent; enable native KVM or prepare the x86_64 PVM host kernel and reboot first"
    fi
  elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    preflight_fail "/dev/kvm exists but is not readable and writable"
  fi

  if [[ -n "${CUBE_SANDBOX_NODE_IP:-}" ]]; then
    python3 - "${CUBE_SANDBOX_NODE_IP}" <<'PY' \
      || preflight_fail "CUBE_SANDBOX_NODE_IP must be a valid IPv4 address"
import ipaddress
import sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
  else
    preflight_fail "CUBE_SANDBOX_NODE_IP could not be auto-detected; set it explicitly"
  fi
  local global_ip_count
  global_ip_count="$(ip -4 -o addr show scope global 2>/dev/null | wc -l || true)"
  if [[ "${global_ip_count}" =~ ^[0-9]+$ ]] && (( global_ip_count > 1 )) && (( NODE_IP_EXPLICIT == 0 )); then
    preflight_fail "multiple host IPv4 addresses detected; ask the user to confirm and explicitly set CUBE_SANDBOX_NODE_IP"
  fi

  validate_install_root preflight
  if [[ "$(install_root_state)" != "installed" ]]; then
    check_sandbox_cidr
    check_listening_ports
  fi

  if (( PREFLIGHT_FAILURES > 0 )); then
    return 1
  fi
  log "host preflight passed; release architecture=${RELEASE_ARCH}, CUBE_PVM_ENABLE=${CUBE_PVM_ENABLE}"
}

if ! run_host_preflight; then
  die "host preflight failed; resolve the reported items instead of bypassing checks"
fi

RESOLVED_VERSION="${CUBE_VERSION}"
ASSET_NAME=""
DOWNLOAD_URL="${CUBE_SANDBOX_DOWNLOAD_URL:-}"
FALLBACK_URL=""
EXPECTED_SHA256="${CUBE_SANDBOX_SHA256:-}"

if [[ -z "${DOWNLOAD_URL}" ]]; then
  [[ -z "${CUBE_SANDBOX_SHA256:-}" ]] \
    || die "CUBE_SANDBOX_SHA256 requires CUBE_SANDBOX_DOWNLOAD_URL"
  RELEASE_MIRROR="github"
  [[ "${MIRROR}" == "cn" ]] && RELEASE_MIRROR="cn"
  log "discovering ${CUBE_VERSION} ${RELEASE_ARCH} release via ${RELEASE_MIRROR}"
  RELEASE_RECORD="$(python3 "${DISCOVER_RELEASE}" \
    --version "${CUBE_VERSION}" --arch "${RELEASE_ARCH}" --mirror "${RELEASE_MIRROR}")" \
    || die "release discovery failed; check network/proxy settings or provide a verified download URL and SHA-256"
  IFS=$'\x1e' read -r RESOLVED_VERSION ASSET_NAME DOWNLOAD_URL FALLBACK_URL EXPECTED_SHA256 <<<"${RELEASE_RECORD}"
else
  ASSET_NAME="${DOWNLOAD_URL##*/}"
  ASSET_NAME="${ASSET_NAME%%\?*}"
  ASSET_NAME="${ASSET_NAME%%\#*}"
  [[ "${ASSET_NAME}" == cube-sandbox-one-click-*-"${RELEASE_ARCH}".tar.gz ]] \
    || die "download filename must be cube-sandbox-one-click-<version>-${RELEASE_ARCH}.tar.gz"
  RESOLVED_VERSION="${ASSET_NAME#cube-sandbox-one-click-}"
  RESOLVED_VERSION="${RESOLVED_VERSION%-${RELEASE_ARCH}.tar.gz}"
  [[ -n "${EXPECTED_SHA256}" ]] \
    || warn "an explicit URL without CUBE_SANDBOX_SHA256 is not integrity-pinned; use only a trusted source"
fi

[[ -n "${RESOLVED_VERSION}" && -n "${ASSET_NAME}" && -n "${DOWNLOAD_URL}" ]] \
  || die "release helper returned an incomplete record"
[[ "${EXPECTED_SHA256}" =~ ^([0-9a-fA-F]{64})?$ ]] \
  || die "CUBE_SANDBOX_SHA256 must be exactly 64 hexadecimal characters"
EXPECTED_SHA256="${EXPECTED_SHA256,,}"

redact_url() {
  CUBE_URL_TO_REDACT="$1" python3 - <<'PY'
import os
import urllib.parse

url = os.environ["CUBE_URL_TO_REDACT"]
parts = urllib.parse.urlsplit(url)
if parts.scheme not in ("http", "https") or not parts.hostname:
    raise SystemExit(1)
host = "[" + parts.hostname + "]" if ":" in parts.hostname else parts.hostname
if parts.port:
    host += ":" + str(parts.port)
print(parts.scheme + "://" + host)
PY
}
DOWNLOAD_ORIGIN="$(redact_url "${DOWNLOAD_URL}")" || die "download URL must be a valid HTTP(S) URL"
log "selected ${RESOLVED_VERSION}: ${ASSET_NAME}"
log "download source: ${DOWNLOAD_ORIGIN} (credentials, path, and query redacted)"

if (( PREFLIGHT_ONLY == 1 )); then
  log "preflight-only complete; no host configuration was changed and no bundle was downloaded"
  exit 0
fi

# Serialize users of this wrapper and revalidate immediately before side effects.
DEPLOYMENT_LOCK="/run/lock/cubesandbox-deployment.lock"
exec 9>"${DEPLOYMENT_LOCK}"
flock -n 9 || die "another cubesandbox-deployment process holds ${DEPLOYMENT_LOCK}"
validate_install_root final

prepare_host() {
  if [[ "$(stat -fc %T /sys/fs/bpf 2>/dev/null || true)" != "bpf" ]]; then
    log "mounting /sys/fs/bpf as bpffs"
    mkdir -p /sys/fs/bpf
    mount -t bpf bpf /sys/fs/bpf || die "failed to mount bpffs"
    if ! grep -Eq '^[^#]+[[:space:]]+/sys/fs/bpf[[:space:]]+bpf[[:space:]]' /etc/fstab 2>/dev/null; then
      printf 'bpf /sys/fs/bpf bpf defaults 0 0\n' >> /etc/fstab
    fi
  fi

  if [[ "${CUBE_PVM_ENABLE}" == "1" ]] && ! pvm_module_present; then
    log "loading kvm_pvm on the x86_64 PVM host"
    modprobe kvm_pvm || die "failed to load kvm_pvm"
  fi
  [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]] \
    || die "/dev/kvm is still unavailable or inaccessible after host preparation"
}

prepare_host

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cubesandbox-deployment.XXXXXX")"
BUNDLE_ARCHIVE="${WORK_DIR}/bundle.tar.gz"

download_bundle() {
  local url="$1" config rc=0
  [[ "${url}" != *$'\n'* && "${url}" != *$'\r'* && "${url}" != *'"'* && "${url}" != *'\\'* ]] \
    || die "download URL contains unsupported control or quoting characters"
  config="$(mktemp "${WORK_DIR}/curl-config.XXXXXX")"
  chmod 600 "${config}"
  # A root-only curl config keeps signed URLs out of curl's process arguments.
  printf 'url = "%s"\n' "${url}" >"${config}"
  curl --config "${config}" --fail --location --silent --show-error \
    --retry 3 --retry-delay 2 --connect-timeout 20 --output "${BUNDLE_ARCHIVE}" || rc=$?
  rm -f "${config}"
  return "${rc}"
}

log "downloading ${ASSET_NAME}"
if ! download_bundle "${DOWNLOAD_URL}"; then
  if [[ -n "${FALLBACK_URL}" && "${FALLBACK_URL}" != "${DOWNLOAD_URL}" ]]; then
    warn "primary download failed; retrying the same verified asset from GitHub"
    DOWNLOAD_URL="${FALLBACK_URL}"
    download_bundle "${DOWNLOAD_URL}" || die "bundle download failed from both mirrors"
  else
    die "bundle download failed"
  fi
fi

if [[ -n "${EXPECTED_SHA256}" ]]; then
  ACTUAL_SHA256="$(sha256sum "${BUNDLE_ARCHIVE}" | awk '{ print $1 }')"
  [[ "${ACTUAL_SHA256}" == "${EXPECTED_SHA256}" ]] \
    || die "SHA-256 mismatch: expected ${EXPECTED_SHA256}, got ${ACTUAL_SHA256}"
  log "SHA-256 verified"
fi

log "extracting and validating the official bundle"
tar -xzf "${BUNDLE_ARCHIVE}" -C "${WORK_DIR}"
BUNDLE_DIR="${WORK_DIR}/${ASSET_NAME%.tar.gz}"
[[ -d "${BUNDLE_DIR}" && -f "${BUNDLE_DIR}/install.sh" ]] \
  || die "unexpected bundle layout: expected ${ASSET_NAME%.tar.gz}/install.sh"

# Pass intentional overrides through the upstream-supported dotenv mechanism.
# Wrapper-only settings and secrets are not persisted into the runtime config.
UPSTREAM_ENV_FILE="${WORK_DIR}/upstream-deployment.env"
CUBE_PARSED_ENV="${PARSED_ENV_FILE}" CUBE_UPSTREAM_OUTPUT="${UPSTREAM_ENV_FILE}" \
CUBE_RESOLVED_PVM="${CUBE_PVM_ENABLE}" CUBE_RESOLVED_NODE_IP="${CUBE_SANDBOX_NODE_IP:-}" \
python3 - <<'PY'
import os
import shlex

parsed_path = os.environ.get("CUBE_PARSED_ENV", "")
output = os.environ["CUBE_UPSTREAM_OUTPUT"]
excluded = {
    "CUBE_VERSION", "CUBE_SANDBOX_DOWNLOAD_URL", "CUBE_SANDBOX_SHA256",
    "CUBE_GITHUB_API_BASE", "CUBE_SANDBOX_INSTALL_ROOT", "GITHUB_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "CUBE_PVM_ENABLE", "CUBE_SANDBOX_NODE_IP", "ONE_CLICK_DEPLOY_ROLE",
    "ONE_CLICK_RUN_QUICKCHECK", "ONE_CLICK_MODE", "ONE_CLICK_ASSUME_YES",
    "ONE_CLICK_ENV_FILE", "ONE_CLICK_SKIP_PRECHECK", "ONE_CLICK_SKIP_PVM_CHECK",
    "ONE_CLICK_ALLOW_DOWNGRADE", "ONE_CLICK_ALLOW_ROLE_CHANGE", "ONE_CLICK_PACKAGE_TAR",
    "CUBE_API_HEALTH_ADDR", "CUBE_OPS_HEALTH_ADDR",
    "CUBE_OPS_UPSTREAM", "WEB_UI_UPSTREAM",
}
lines = []
entries = []
if parsed_path:
    with open(parsed_path, "rb") as handle:
        data = handle.read()
    if data:
        fields = data.split(b"\0")
        if fields[-1] != b"" or (len(fields) - 1) % 2:
            raise SystemExit("invalid parsed environment record")
        fields = fields[:-1]
        entries = [
            (fields[index].decode("ascii"), fields[index + 1].decode("utf-8"))
            for index in range(0, len(fields), 2)
        ]
for key, value in entries:
    if key not in excluded:
        lines.append(key + "=" + shlex.quote(value))
for key, value in (
    ("CUBE_PVM_ENABLE", os.environ["CUBE_RESOLVED_PVM"]),
    ("CUBE_SANDBOX_NODE_IP", os.environ["CUBE_RESOLVED_NODE_IP"]),
    ("ONE_CLICK_DEPLOY_ROLE", "control"),
    ("ONE_CLICK_RUN_QUICKCHECK", "1"),
    ("CUBE_API_HEALTH_ADDR", os.environ["CUBE_API_HEALTH_ADDR"]),
    ("CUBE_OPS_HEALTH_ADDR", os.environ["CUBE_OPS_HEALTH_ADDR"]),
    ("CUBE_OPS_UPSTREAM", os.environ["CUBE_OPS_UPSTREAM"]),
    ("WEB_UI_UPSTREAM", os.environ["WEB_UI_UPSTREAM"]),
):
    if value:
        lines.append(key + "=" + shlex.quote(value))
with open(output, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.chmod(output, 0o600)
PY
if [[ -n "${PARSED_ENV_FILE}" ]]; then
  rm -f "${PARSED_ENV_FILE}"
  PARSED_ENV_FILE=""
fi
export ONE_CLICK_ENV_FILE="${UPSTREAM_ENV_FILE}"

ONE_CLICK_MODE="install"
export ONE_CLICK_MODE
export ONE_CLICK_ASSUME_YES=1
export ONE_CLICK_RUN_QUICKCHECK=1
export ONE_CLICK_DEPLOY_ROLE=control
export MIRROR
unset ONE_CLICK_SKIP_PRECHECK ONE_CLICK_SKIP_PVM_CHECK ONE_CLICK_PACKAGE_TAR
unset ONE_CLICK_ALLOW_DOWNGRADE ONE_CLICK_ALLOW_ROLE_CHANGE

# An administrator not using this wrapper may have installed during download.
validate_install_root final
log "running official install.sh in ${ONE_CLICK_MODE} mode (service health quickcheck enabled)"
(
  cd "${BUNDLE_DIR}"
  bash ./install.sh
)

[[ -f "${INSTALL_ROOT}/.one-click.env" ]] \
  || die "official installer returned without creating ${INSTALL_ROOT}/.one-click.env"

log "deployment completed successfully"
log "version=${RESOLVED_VERSION}; architecture=${HOST_ARCH}; release_arch=${RELEASE_ARCH}; PVM=${CUBE_PVM_ENABLE}"
log "install root: ${INSTALL_ROOT}"
API_PORT="$(bind_port "${CUBE_API_BIND:-0.0.0.0:3000}")"
log "CubeAPI: http://127.0.0.1:${API_PORT} (remote access: http://${CUBE_SANDBOX_NODE_IP:-<node-ip>}:${API_PORT})"
if [[ "${WEB_UI_ENABLE:-1}" == "1" ]]; then
  log "WebUI: http://${CUBE_SANDBOX_NODE_IP:-<node-ip>}:${WEB_UI_HOST_PORT:-12088}"
fi
log "no template was created and no sandbox workload or benchmark was run"
