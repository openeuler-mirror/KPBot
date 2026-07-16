#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEFAULT_REPO_SKILL_DIR="${REPO_ROOT}/ref-skills/network-io-performance"
LEGACY_FALLBACK_DIR=""  # Removed hardcoded path; use CLI arg or repo-local subskill

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  EXTERNAL_SKILL_DIR="${1}"
elif [[ -f "${DEFAULT_REPO_SKILL_DIR}/scripts/network_io_check.sh" ]]; then
  EXTERNAL_SKILL_DIR="${DEFAULT_REPO_SKILL_DIR}"
elif [[ -n "${LEGACY_FALLBACK_DIR}" && -f "${LEGACY_FALLBACK_DIR}/scripts/network_io_check.sh" ]]; then
  EXTERNAL_SKILL_DIR="${LEGACY_FALLBACK_DIR}"
else
  echo "No external network IO skill found. Provide path as argument or install to ref-skills/." >&2
  exit 1
fi

MAIN_SCRIPT="${EXTERNAL_SKILL_DIR}/scripts/network_io_check.sh"

if [[ ! -f "${MAIN_SCRIPT}" ]]; then
  echo "missing external network IO script: ${MAIN_SCRIPT}" >&2
  exit 1
fi

bash "${MAIN_SCRIPT}"
