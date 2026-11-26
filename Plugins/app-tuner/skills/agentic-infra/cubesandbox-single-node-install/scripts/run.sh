#!/usr/bin/env bash
# Authorized fresh-install-only entry; all host safety checks stay in deploy.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARED_SCRIPTS="$(cd "${SKILL_DIR}/.." && pwd)/cubesandbox-deployment/scripts"
LOG_PREFIX="[cubesandbox-single-node-install]"
ENV_FILE=""
EXPECTED_VERSION=""
CONFIRMED=0

usage() {
  cat <<'USAGE'
Usage: bash scripts/run.sh --confirm-fresh-install --expect-version <preflight-release.version> [--env <deployment.env>]

Execute a new single-node all-in-one installation after explicit user
authorization. --expect-version must exactly equal the approved preflight
report's release.version. This entry cannot select preflight, verification,
upgrade, recovery, offline, multi-node, or safety-bypass modes.
Exit codes: 0=installed and official quickcheck passed, nonzero=not successful;
4=blocked because the shared implementation is unavailable.
USAGE
}

die_usage() {
  printf '%s ERROR: %s\n' "${LOG_PREFIX}" "$*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-fresh-install)
      CONFIRMED=1
      shift
      ;;
    --expect-version)
      [[ $# -ge 2 ]] || die_usage "--expect-version requires the approved preflight release.version"
      EXPECTED_VERSION="$2"
      shift 2
      ;;
    --env)
      [[ $# -ge 2 ]] || die_usage "--env requires a file path"
      ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "unsupported option '$1'; this entry only performs an authorized fresh single-node install"
      ;;
  esac
done
[[ "${CONFIRMED}" -eq 1 ]] \
  || die_usage "explicit authorization is required: pass --confirm-fresh-install only after the user approves the change summary"
[[ -n "${EXPECTED_VERSION}" ]] \
  || die_usage "--expect-version is required; copy release.version from the approved ready preflight report"
[[ "${EXPECTED_VERSION}" =~ ^v[0-9A-Za-z][0-9A-Za-z._-]*$ ]] \
  || die_usage "--expect-version must be an exact release tag, not latest or a version range"

for forbidden_switch in ONE_CLICK_SKIP_PRECHECK ONE_CLICK_SKIP_PVM_CHECK; do
  if [[ "${!forbidden_switch:-0}" != "0" ]]; then
    printf '%s ERROR: %s is forbidden; safety checks cannot be bypassed\n' \
      "${LOG_PREFIX}" "${forbidden_switch}" >&2
    exit 2
  fi
done
if [[ "${ONE_CLICK_RUN_QUICKCHECK:-1}" != "1" ]]; then
  printf '%s ERROR: disabling the official quickcheck is forbidden\n' "${LOG_PREFIX}" >&2
  exit 2
fi
if [[ "${ONE_CLICK_MODE:-install}" != "install" || -n "${ONE_CLICK_PACKAGE_TAR:-}" ]]; then
  printf '%s ERROR: install mode and the verified official bundle are managed by the shared deploy.sh\n' \
    "${LOG_PREFIX}" >&2
  exit 2
fi

for helper in deploy.sh discover_release.py parse_env.py; do
  if [[ ! -f "${SHARED_SCRIPTS}/${helper}" ]]; then
    printf '%s ERROR: blocked: missing shared implementation %s/%s; reinstall cubesandbox-deployment alongside this skill\n' \
      "${LOG_PREFIX}" "${SHARED_SCRIPTS}" "${helper}" >&2
    exit 4
  fi
done

DEPLOY_ARGS=(--expect-version "${EXPECTED_VERSION}")
if [[ -n "${ENV_FILE}" ]]; then
  DEPLOY_ARGS+=(--env "${ENV_FILE}")
fi
# No caller-controlled deploy.sh flags are forwarded. deploy.sh still performs
# its own preflight, exact-version and existing-install guards, lock/rechecks,
# mandatory integrity checks, official install.sh, and official quickcheck.
exec bash "${SHARED_SCRIPTS}/deploy.sh" "${DEPLOY_ARGS[@]}"
