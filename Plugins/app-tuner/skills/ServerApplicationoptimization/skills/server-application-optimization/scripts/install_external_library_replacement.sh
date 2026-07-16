#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
DEFAULT_REPO_SKILL_DIR="${REPO_ROOT}/ref-skills/library-replacement"
TARGET_ROOT="${REPO_ROOT}/subskill"
REPO_URL="git@gitee.com:KunpengSDK/skills.git"
ALLOW_CLONE=false
TARGET_DIR="${TARGET_ROOT}/library-replacement"
REPO_DIR="${TARGET_ROOT}"
KB_PATH="${TARGET_DIR}/optimization_kb.json"

usage() {
  cat <<'EOF'
Usage:
  install_external_library_replacement.sh [--target-root <dir>] [--repo-url <url>] [--allow-clone]

Default behavior is offline-only. The script prefers the vendored
ref-skills/library-replacement directory and will not clone external sources
unless --allow-clone is explicitly provided after security review.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-root) TARGET_ROOT="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --allow-clone) ALLOW_CLONE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[fail] unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

TARGET_DIR="${TARGET_ROOT}/library-replacement"
REPO_DIR="${TARGET_ROOT}"
KB_PATH="${TARGET_DIR}/optimization_kb.json"

if [[ -f "${DEFAULT_REPO_SKILL_DIR}/SKILL.md" ]]; then
  echo "[ok] vendored library-replacement found at ${DEFAULT_REPO_SKILL_DIR}"
  if [[ -f "${DEFAULT_REPO_SKILL_DIR}/optimization_kb.json" ]]; then
    echo "[ok] optimization_kb.json found at ${DEFAULT_REPO_SKILL_DIR}/optimization_kb.json"
  else
    echo "[warn] optimization_kb.json not found in vendored subskill"
    echo "[next] place optimization_kb.json under ${DEFAULT_REPO_SKILL_DIR} or pass an explicit optimization_kb_path when invoking the framework"
  fi
  for cmd in perf lsof ps uname lscpu; do
    if command -v "${cmd}" >/dev/null 2>&1; then
      echo "[ok] command available: ${cmd}"
    else
      echo "[warn] command missing: ${cmd}"
    fi
  done
  echo "[done] vendored subskill is already available. Legacy external installation is optional."
  exit 0
fi

mkdir -p "${TARGET_ROOT}"

if [[ "${ALLOW_CLONE}" != true ]]; then
  echo "[warn] vendored subskill not found at ${DEFAULT_REPO_SKILL_DIR}"
  echo "[blocked] external clone is disabled by default. Re-run with --allow-clone only after reviewing ${REPO_URL}."
  exit 2
fi

if [[ ! -d "${REPO_DIR}/.git" ]]; then
  echo "[info] vendored subskill not found, cloning reviewed fallback from ${REPO_URL} into ${REPO_DIR}"
  git clone "${REPO_URL}" "${REPO_DIR}"
else
  echo "[info] fallback repository already exists at ${REPO_DIR}, skip clone"
fi

if [[ -f "${TARGET_DIR}/SKILL.md" ]]; then
  echo "[ok] library-replacement skill found at ${TARGET_DIR}"
else
  echo "[warn] library-replacement directory or SKILL.md not found under ${TARGET_DIR}"
fi

if [[ -f "${KB_PATH}" ]]; then
  echo "[ok] optimization_kb.json found at ${KB_PATH}"
else
  echo "[warn] optimization_kb.json not found"
  echo "[next] please place optimization_kb.json at ${KB_PATH} or pass an explicit optimization_kb_path when invoking the framework"
fi

for cmd in perf lsof ps uname lscpu; do
  if command -v "${cmd}" >/dev/null 2>&1; then
    echo "[ok] command available: ${cmd}"
  else
    echo "[warn] command missing: ${cmd}"
  fi
done

echo "[done] installation helper finished. No system packages were installed automatically. This compatibility path is only used when repo-local subskill is absent."
