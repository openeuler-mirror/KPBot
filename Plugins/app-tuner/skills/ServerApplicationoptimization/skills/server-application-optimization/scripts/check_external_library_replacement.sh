#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEFAULT_REPO_SKILL_DIR="${REPO_ROOT}/ref-skills/library-replacement"
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

OPT_KB_PATH="${2:-${EXTERNAL_SKILL_DIR}/optimization_kb.json}"

raw_arch="$(uname -m 2>/dev/null || echo unknown)"
arch="${raw_arch}"
if [[ "${arch}" == "arm64" ]]; then
  arch="aarch64"
fi
skill_file="${EXTERNAL_SKILL_DIR}/SKILL.md"

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

perf_status="missing"
lsof_status="missing"
ps_status="missing"
readelf_status="missing"
jemalloc_status="missing"
tcmalloc_status="missing"
libmem_status="missing"
external_library_availability="unknown"
external_script_status="unknown"

have_cmd perf && perf_status="available"
have_cmd lsof && lsof_status="available"
have_cmd ps && ps_status="available"
have_cmd readelf && readelf_status="available"

if ls /usr/lib64/libjemalloc.so* /usr/lib*/libjemalloc.so* >/dev/null 2>&1; then
  jemalloc_status="available"
fi

if ls /usr/lib64/libtcmalloc*.so* /usr/lib*/libtcmalloc*.so* >/dev/null 2>&1; then
  tcmalloc_status="available"
fi

if ls /usr/lib64/libmem.so* /usr/lib*/libmem.so* >/dev/null 2>&1; then
  libmem_status="available"
fi

skill_exists=false
kb_exists=false
supported_arch=false

[[ -f "${skill_file}" ]] && skill_exists=true
[[ -f "${OPT_KB_PATH}" ]] && kb_exists=true
[[ "${arch}" == "aarch64" ]] && supported_arch=true

install_status="ready"
fallback_reason=""

if [[ "${supported_arch}" != true ]]; then
  install_status="not_applicable"
  fallback_reason="architecture_not_aarch64"
elif [[ "${skill_exists}" != true ]]; then
  install_status="missing_external_skill"
  fallback_reason="external_skill_not_installed"
elif [[ "${kb_exists}" != true ]]; then
  install_status="missing_knowledge_base"
  fallback_reason="optimization_kb_missing"
fi

if [[ "${jemalloc_status}" == "available" || "${tcmalloc_status}" == "available" || "${libmem_status}" == "available" ]]; then
  external_library_availability="replacement_candidates_available"
else
  external_library_availability="no_replacement_candidates_found"
fi

if [[ -f "${EXTERNAL_SKILL_DIR}/scripts/detect_all_libraries.sh" ]]; then
  external_script_status="present_but_unverified"
fi

cat <<EOF
{
  "architecture": "${arch}",
  "raw_architecture": "${raw_arch}",
  "external_skill_dir": "${EXTERNAL_SKILL_DIR}",
  "external_skill_file": "${skill_file}",
  "optimization_kb_path": "${OPT_KB_PATH}",
  "skill_source": "${skill_source}",
  "supported_arch": ${supported_arch},
  "external_skill_exists": ${skill_exists},
  "optimization_kb_exists": ${kb_exists},
  "tool_status": {
    "perf": "${perf_status}",
    "lsof": "${lsof_status}",
    "ps": "${ps_status}",
    "readelf": "${readelf_status}"
  },
  "replacement_library_status": {
    "jemalloc": "${jemalloc_status}",
    "tcmalloc": "${tcmalloc_status}",
    "libmem": "${libmem_status}"
  },
  "external_library_availability": "${external_library_availability}",
  "external_script_status": "${external_script_status}",
  "installation_status": "${install_status}",
  "fallback_reason": "${fallback_reason}",
  "mysql_library_replacement_notes": [
    "Prefer WITH_JEMALLOC=system when build-time integration is available",
    "Use LD_PRELOAD only as a runtime fallback when build-time integration is not possible",
    "If external script reports report file not found, treat it as external execution failure and fall back to internal analysis"
  ]
}
EOF
