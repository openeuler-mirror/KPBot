#!/usr/bin/env bash
# Independently verify an installed all-in-one node via its official quickcheck.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT_SAFETY="${SCRIPT_DIR}/report_safety.py"
INSTALL_ROOT="/usr/local/services/cubetoolbox"
QUICKCHECK="${INSTALL_ROOT}/scripts/one-click/quickcheck.sh"
RUNTIME_ENV="${INSTALL_ROOT}/.one-click.env"
DEFAULT_RUNTIME_DIR="/var/run/cube-sandbox-one-click"
DEFAULT_LOG_DIR="/var/log/cube-sandbox-one-click"
LOG_PREFIX="[cubesandbox-deployment-verify]"
SKILL_NAME="cubesandbox-deployment-verify"
REPORT_FILE=""
LOG_FILE=""

cleanup() {
  if [[ -n "${LOG_FILE}" ]]; then
    rm -f "${LOG_FILE}"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'USAGE'
Usage: bash scripts/verify.sh --report <json-file>

Run only the installed official all-in-one quickcheck and require its systemd,
CubeAPI, CubeOps, and completion evidence. No Release discovery, download,
installation, restart, directory creation, or configuration write is performed.
The only file written is the requested JSON report.
Exit codes: 0=healthy, 1=health verification failed, 2=usage error, 4=blocked.
USAGE
}

fail_usage() {
  printf '%s ERROR: %s\n' "${LOG_PREFIX}" "$*" >&2
  usage >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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
      fail_usage "unknown option for independent read-only verification: $1"
      ;;
  esac
done
[[ -n "${REPORT_FILE}" ]] || fail_usage "--report is required"

if [[ ! -f "${REPORT_SAFETY}" ]]; then
  printf '%s ERROR: blocked: missing safe report implementation %s; cannot safely write a report\n' \
    "${LOG_PREFIX}" "${REPORT_SAFETY}" >&2
  exit 4
fi
if ! python3 "${REPORT_SAFETY}" check "${REPORT_FILE}" "${SKILL_NAME}" verify \
  --install-root "${INSTALL_ROOT}"; then
  printf '%s ERROR: blocked: unsafe JSON report path %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  exit 4
fi

write_report() {
  local status="$1" quickcheck_rc="$2" runtime_state="$3" quickcheck_state="$4" health_state="$5" reason="$6"
  python3 - "${REPORT_FILE}" "${status}" "${quickcheck_rc}" "${runtime_state}" \
    "${quickcheck_state}" "${health_state}" "${reason}" "${LOG_FILE}" \
    "${REPORT_SAFETY}" "${INSTALL_ROOT}" <<'PY'
import os
from pathlib import Path
import sys

(path, status, quickcheck_rc, runtime_state, quickcheck_state,
 health_state, reason, log_name, safety_helper, install_root) = sys.argv[1:]
sys.path.insert(0, str(Path(safety_helper).parent))
sys.dont_write_bytecode = True
from report_safety import atomic_write_report

text = ""
if log_name and os.path.isfile(log_name):
    text = Path(log_name).read_text(encoding="utf-8", errors="replace")
markers = {
    "control_role": "[quickcheck] role=control" in text,
    "systemd_units": "[quickcheck] check systemd units" in text,
    "cube_api_health": "[quickcheck] 3/5 check cube-api /health" in text,
    "cube_ops_health": "[quickcheck] 4/5 check cubeops /health" in text,
    "official_completion": "[quickcheck] OK" in text,
}
report = {
    "schema_version": 1,
    "skill": "cubesandbox-deployment-verify",
    "mode": "verify",
    "status": status,
    "success": status == "success",
    "quickcheck_exit_code": None if quickcheck_rc == "none" else int(quickcheck_rc),
    "checks": {
        "runtime_env": runtime_state,
        "official_quickcheck": quickcheck_state,
        "services_and_health_endpoints": health_state,
    },
    "official_evidence": markers,
    "reason": reason,
    "side_effects": {
        "release_discovery": False,
        "bundle_downloaded": False,
        "installation_or_restart": False,
        "host_configuration_changed": False,
    },
}
atomic_write_report(path, report, install_root=install_root)
PY
}

finish() {
  local exit_code="$1"
  shift
  if ! write_report "$@"; then
    printf '%s ERROR: blocked: could not safely write JSON report %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
    exit 4
  fi
  printf '%s JSON report: %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  exit "${exit_code}"
}

if [[ "${EUID}" -ne 0 ]]; then
  printf '%s ERROR: blocked: root is required to read the protected runtime configuration\n' "${LOG_PREFIX}" >&2
  finish 4 blocked none blocked blocked blocked "root privileges are unavailable"
fi
if [[ ! -f "${RUNTIME_ENV}" || ! -r "${RUNTIME_ENV}" || -L "${RUNTIME_ENV}" ]]; then
  printf '%s ERROR: blocked: readable regular runtime file is missing: %s\n' "${LOG_PREFIX}" "${RUNTIME_ENV}" >&2
  finish 4 blocked none blocked blocked blocked "the official .one-click.env is absent or unreadable"
fi
if [[ ! -f "${QUICKCHECK}" || ! -r "${QUICKCHECK}" || -L "${QUICKCHECK}" \
  || ! -f "${INSTALL_ROOT}/scripts/one-click/common.sh" ]]; then
  printf '%s ERROR: blocked: installed official quickcheck is missing or incomplete: %s\n' \
    "${LOG_PREFIX}" "${QUICKCHECK}" >&2
  finish 4 blocked none passed blocked blocked "the installed official quickcheck or its common library is unavailable"
fi

# v0.7.3 common.sh sources the runtime env, then runs mkdir -p for these two
# directories. Resolve only their non-secret assignments without sourcing or
# printing the protected env; every other line and value is ignored.
if ! RESOLVED_DIRS="$(python3 - "${RUNTIME_ENV}" "${DEFAULT_RUNTIME_DIR}" "${DEFAULT_LOG_DIR}" <<'PY'
import os
import re
import shlex
import sys

runtime_env, runtime_default, log_default = sys.argv[1:]
defaults = {
    "ONE_CLICK_RUNTIME_DIR": runtime_default,
    "ONE_CLICK_LOG_DIR": log_default,
}
values = {key: os.environ.get(key) or default for key, default in defaults.items()}
assignment = re.compile(r"^(?:export[ \t]+)?(ONE_CLICK_RUNTIME_DIR|ONE_CLICK_LOG_DIR)=(.*)$")
with open(runtime_env, "r", encoding="utf-8", newline="") as handle:
    for line_number, raw_line in enumerate(handle, 1):
        line = raw_line.rstrip("\r\n")
        match = assignment.fullmatch(line)
        if not match:
            continue
        key, raw_value = match.groups()
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise SystemExit("invalid %s quoting on line %d: %s" % (key, line_number, exc))
        if len(parsed) > 1:
            raise SystemExit("%s on line %d must contain one quoted path" % (key, line_number))
        values[key] = (parsed[0] if parsed else "") or defaults[key]
for key, value in values.items():
    if not os.path.isabs(value) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SystemExit("%s must be an absolute path without control characters" % key)
    if "$" in value or "`" in value:
        raise SystemExit("%s with shell expansion is unsupported for read-only verification" % key)
print("\x1e".join((values["ONE_CLICK_RUNTIME_DIR"], values["ONE_CLICK_LOG_DIR"])))
PY
)"; then
  printf '%s ERROR: blocked: could not resolve official runtime/log directories without evaluating runtime secrets\n' \
    "${LOG_PREFIX}" >&2
  finish 4 blocked none passed blocked blocked "the official runtime or log directory configuration is unsafe or unreadable"
fi
IFS=$'\x1e' read -r EFFECTIVE_RUNTIME_DIR EFFECTIVE_LOG_DIR <<<"${RESOLVED_DIRS}"
if [[ ! -d "${EFFECTIVE_RUNTIME_DIR}" || ! -d "${EFFECTIVE_LOG_DIR}" ]]; then
  printf '%s ERROR: blocked: official runtime and log directories must already exist; verification will not create them\n' \
    "${LOG_PREFIX}" >&2
  finish 4 blocked none passed blocked blocked "ONE_CLICK_RUNTIME_DIR or ONE_CLICK_LOG_DIR is absent or not a directory"
fi

LOG_FILE="$(mktemp "${TMPDIR:-/tmp}/cubesandbox-verify.XXXXXX")" || exit 4
(
  cd "${INSTALL_ROOT}" || exit 125
  export ONE_CLICK_ENV_FILE="${RUNTIME_ENV}"
  export ONE_CLICK_RUNTIME_ENV_FILE="${RUNTIME_ENV}"
  export ONE_CLICK_RUNTIME_DIR="${EFFECTIVE_RUNTIME_DIR}"
  export ONE_CLICK_LOG_DIR="${EFFECTIVE_LOG_DIR}"
  export CUBE_SANDBOX_INSTALL_ROOT="${INSTALL_ROOT}"
  export ONE_CLICK_RUN_QUICKCHECK=1
  unset ONE_CLICK_MODE ONE_CLICK_ASSUME_YES ONE_CLICK_PACKAGE_TAR
  unset ONE_CLICK_SKIP_PRECHECK ONE_CLICK_SKIP_PVM_CHECK
  unset ONE_CLICK_ALLOW_DOWNGRADE ONE_CLICK_ALLOW_ROLE_CHANGE
  bash "${QUICKCHECK}"
) >"${LOG_FILE}" 2>&1
QUICKCHECK_RC=$?
# Keep useful official diagnostics while removing every HTTP(S) userinfo, path,
# and query component. The raw log remains root-only temporary data, is used
# only for fixed evidence markers, and is removed on exit.
python3 - "${LOG_FILE}" <<'PY'
import re
from pathlib import Path
import sys
import urllib.parse

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")


def url_origin(match):
    raw = match.group(0)
    try:
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return "<redacted-url>"
        host = "[" + parts.hostname + "]" if ":" in parts.hostname else parts.hostname
        if parts.port:
            host += ":" + str(parts.port)
        return parts.scheme.lower() + "://" + host
    except ValueError:
        return "<redacted-url>"


text = re.sub(r"(?i)https?://[^\s<>\"']+", url_origin, text)
text = re.sub(
    r"(?i)\b(password|passwd|token|secret|private[_-]?key)=([^\s]+)",
    lambda match: match.group(1) + "=<redacted>",
    text,
)
sys.stderr.write(text)
PY

if [[ "${QUICKCHECK_RC}" -ne 0 ]]; then
  printf '%s ERROR: official quickcheck failed; deployment is not healthy\n' "${LOG_PREFIX}" >&2
  finish 1 failed "${QUICKCHECK_RC}" passed failed failed "the official quickcheck returned nonzero"
fi

required_markers=(
  '[quickcheck] role=control'
  '[quickcheck] check systemd units'
  '[quickcheck] 3/5 check cube-api /health'
  '[quickcheck] 4/5 check cubeops /health'
  '[quickcheck] OK'
)
for marker in "${required_markers[@]}"; do
  if ! grep -Fq "${marker}" "${LOG_FILE}"; then
    printf '%s ERROR: blocked: official quickcheck completed without required evidence: %s\n' \
      "${LOG_PREFIX}" "${marker}" >&2
    finish 4 blocked "${QUICKCHECK_RC}" passed blocked blocked "official all-in-one service/health evidence is incomplete"
  fi
done
finish 0 success "${QUICKCHECK_RC}" passed passed passed "official all-in-one quickcheck and runtime environment passed"
