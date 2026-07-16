#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEFAULT_REPO_SKILL_DIR="${REPO_ROOT}/ref-skills/network-io-performance"
LEGACY_FALLBACK_DIR=""  # Removed hardcoded path; use CLI arg or repo-local subskill

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  EXTERNAL_SKILL_DIR="${1}"
  skill_source="external_override"
elif [[ -f "${DEFAULT_REPO_SKILL_DIR}/SKILL.md" ]]; then
  EXTERNAL_SKILL_DIR="${DEFAULT_REPO_SKILL_DIR}"
  skill_source="repo_local_subskill"
elif [[ -n "${LEGACY_FALLBACK_DIR}" && -f "${LEGACY_FALLBACK_DIR}/SKILL.md" ]]; then
  EXTERNAL_SKILL_DIR="${LEGACY_FALLBACK_DIR}"
  skill_source="external_fallback"
else
  EXTERNAL_SKILL_DIR=""
  skill_source="unavailable"
fi

skill_file="${EXTERNAL_SKILL_DIR}/SKILL.md"
main_script="${EXTERNAL_SKILL_DIR}/scripts/network_io_check.sh"

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

ip_status="missing"
sar_status="missing"
netstat_status="missing"
ethtool_status="missing"
irqtop_status="missing"

have_cmd ip && ip_status="available"
have_cmd sar && sar_status="available"
have_cmd netstat && netstat_status="available"
have_cmd ethtool && ethtool_status="available"
have_cmd irqtop && irqtop_status="available"

skill_exists=false
script_exists=false

[[ -f "${skill_file}" ]] && skill_exists=true
[[ -f "${main_script}" ]] && script_exists=true

install_status="ready"
fallback_reason=""

if [[ "${skill_exists}" != true ]]; then
  install_status="missing_external_skill"
  fallback_reason="external_network_skill_not_installed"
elif [[ "${script_exists}" != true ]]; then
  install_status="missing_external_script"
  fallback_reason="network_io_check_script_missing"
elif [[ "${ip_status}" != "available" || "${sar_status}" != "available" || "${netstat_status}" != "available" || "${ethtool_status}" != "available" ]]; then
  install_status="missing_required_tools"
  fallback_reason="network_tool_dependencies_missing"
fi

cat <<EOF
{
  "external_skill_dir": "${EXTERNAL_SKILL_DIR}",
  "external_skill_file": "${skill_file}",
  "external_main_script": "${main_script}",
  "skill_source": "${skill_source}",
  "external_skill_exists": ${skill_exists},
  "external_script_exists": ${script_exists},
  "tool_status": {
    "ip": "${ip_status}",
    "sar": "${sar_status}",
    "netstat": "${netstat_status}",
    "ethtool": "${ethtool_status}",
    "irqtop": "${irqtop_status}"
  },
  "installation_status": "${install_status}",
  "fallback_reason": "${fallback_reason}",
  "notes": [
    "irqtop is recommended but not mandatory",
    "external network skill is used as a read-only analysis backend",
    "if external path or required tools are missing, fall back to internal network-optimization"
  ]
}
EOF
