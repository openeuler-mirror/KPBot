#!/usr/bin/env bash
# Run the shared CubeSandbox fresh-install preflight and emit a JSON verdict.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARED_SKILL_DIR="$(cd "${SKILL_DIR}/.." && pwd)/cubesandbox-deployment"
SHARED_SCRIPTS="${SHARED_SKILL_DIR}/scripts"
REPORT_SAFETY="${SHARED_SCRIPTS}/report_safety.py"
LOG_PREFIX="[cubesandbox-deployment-preflight]"
SKILL_NAME="cubesandbox-deployment-preflight"
ENV_FILE=""
REPORT_FILE=""

usage() {
  cat <<'USAGE'
Usage: bash scripts/run.sh --report <json-file> [--env <deployment.env>]

Run only the shared read-only host/configuration/Release preflight. The JSON
report is the authoritative verdict; no installation or host preparation is
performed. Exit codes: 0=ready, 1=checks failed, 2=usage error, 4=blocked.
USAGE
}

fail_usage() {
  printf '%s ERROR: %s\n' "${LOG_PREFIX}" "$*" >&2
  usage >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      [[ $# -ge 2 ]] || fail_usage "--env requires a file path"
      ENV_FILE="$2"
      shift 2
      ;;
    --report)
      [[ $# -ge 2 ]] || fail_usage "--report requires a JSON file path"
      REPORT_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail_usage "unknown option for read-only preflight: $1"
      ;;
  esac
done
[[ -n "${REPORT_FILE}" ]] || fail_usage "--report is required"

if [[ ! -f "${REPORT_SAFETY}" ]]; then
  printf '%s ERROR: blocked: missing safe report implementation %s; cannot safely write a blocked report\n' \
    "${LOG_PREFIX}" "${REPORT_SAFETY}" >&2
  exit 4
fi
if ! python3 "${REPORT_SAFETY}" check "${REPORT_FILE}" "${SKILL_NAME}" preflight \
  --input-env "${ENV_FILE}"; then
  printf '%s ERROR: blocked: unsafe JSON report path %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  exit 4
fi

write_blocked_report() {
  local reason="$1"
  python3 "${REPORT_SAFETY}" blocked "${REPORT_FILE}" "${SKILL_NAME}" preflight \
    --input-env "${ENV_FILE}" --reason "${reason}"
}

for helper in deploy.sh discover_release.py parse_env.py; do
  if [[ ! -f "${SHARED_SCRIPTS}/${helper}" ]]; then
    reason="blocked: missing shared implementation ${SHARED_SCRIPTS}/${helper}; reinstall cubesandbox-deployment alongside this skill"
    if ! write_blocked_report "${reason}"; then
      printf '%s ERROR: could not safely write blocked report\n' "${LOG_PREFIX}" >&2
    fi
    printf '%s %s\n' "${LOG_PREFIX}" "${reason}" >&2
    exit 4
  fi
done

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cubesandbox-preflight.XXXXXX")" || exit 4
trap 'rm -rf "${TMP_DIR}"' EXIT
LOG_FILE="${TMP_DIR}/deploy.log"
DEPLOY_ARGS=(--preflight-only)
if [[ -n "${ENV_FILE}" ]]; then
  DEPLOY_ARGS+=(--env "${ENV_FILE}")
fi

bash "${SHARED_SCRIPTS}/deploy.sh" "${DEPLOY_ARGS[@]}" >"${LOG_FILE}" 2>&1
DEPLOY_RC=$?
cat "${LOG_FILE}" >&2
FINAL_RC="${DEPLOY_RC}"
if [[ "${DEPLOY_RC}" -eq 0 ]] \
  && grep -Fq 'existing installation detected' "${LOG_FILE}"; then
  # deploy.sh deliberately remains backward compatible; this focused Skill has
  # a fresh-install readiness contract and therefore surfaces it as not ready.
  FINAL_RC=1
fi
if [[ "${FINAL_RC}" -eq 0 ]] && ! grep -Fq 'SHA-256 pinning enforced' "${LOG_FILE}"; then
  FINAL_RC=1
fi
if [[ "${FINAL_RC}" -ne 0 ]]; then
  FINAL_RC=1
fi

if ! python3 - "${REPORT_FILE}" "${LOG_FILE}" "${DEPLOY_RC}" "${FINAL_RC}" \
  "${REPORT_SAFETY}" "${ENV_FILE}" <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
log_path = Path(sys.argv[2])
deploy_rc = int(sys.argv[3])
final_rc = int(sys.argv[4])
sys.path.insert(0, str(Path(sys.argv[5]).parent))
sys.dont_write_bytecode = True
from report_safety import atomic_write_report

text = log_path.read_text(encoding="utf-8", errors="replace")
lines = text.splitlines()
prefix = "[cubesandbox-deployment] "
issues = []
warnings = []
planned_actions = []
release = {"version": "", "asset_name": ""}
host = {"release_arch": "", "pvm": ""}
for line in lines:
    if line.startswith(prefix + "ERROR: "):
        issues.append(line[len(prefix + "ERROR: "):])
    elif line.startswith(prefix + "WARNING: "):
        warnings.append(line[len(prefix + "WARNING: "):])
    elif " AUTO: " in line:
        planned_actions.append(line.split(" AUTO: ", 1)[1])
    match = re.search(r" selected ([^ ]+): (.+)$", line)
    if match:
        release = {"version": match.group(1), "asset_name": match.group(2)}
    match = re.search(r"host preflight passed; release architecture=([^,]+), CUBE_PVM_ENABLE=(.+)$", line)
    if match:
        host = {"release_arch": match.group(1), "pvm": match.group(2)}
existing = any("existing installation detected" in line for line in lines)
if existing and "fresh installation would be refused" not in issues:
    issues.append("an existing installation is present; fresh installation is not ready")
sha256_pinned = any("SHA-256 pinning enforced" in line for line in lines)
if final_rc != 0 and not sha256_pinned and not any("SHA-256" in issue for issue in issues):
    issues.append("a trusted SHA-256 digest was not pinned; resolve release integrity before installation")
ready = final_rc == 0 and sha256_pinned
status = "success" if ready else "failed"
report = {
    "schema_version": 1,
    "skill": "cubesandbox-deployment-preflight",
    "mode": "preflight",
    "status": status,
    "success": ready,
    "ready_for_install": ready,
    "deploy_exit_code": deploy_rc,
    "release": release,
    "host": host,
    "sha256_pinned": sha256_pinned,
    "existing_installation_detected": existing,
    "issues": issues,
    "warnings": warnings,
    "planned_install_actions_not_performed": planned_actions,
    "side_effects": {
        "host_configuration_changed": False,
        "bundle_downloaded": False,
        "installation_started": False,
    },
}
atomic_write_report(path, report, input_env=sys.argv[6])
PY
then
  printf '%s ERROR: blocked: could not safely write JSON report %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  exit 4
fi
printf '%s JSON report: %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
exit "${FINAL_RC}"
