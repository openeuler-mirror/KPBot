#!/usr/bin/env bash
# Locate the single shared verifier in both source and installed flat layouts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARED_SCRIPTS="$(cd "${SKILL_DIR}/.." && pwd)/cubesandbox-deployment/scripts"
SHARED_VERIFY="${SHARED_SCRIPTS}/verify.sh"
REPORT_SAFETY="${SHARED_SCRIPTS}/report_safety.py"
INSTALL_ROOT="/usr/local/services/cubetoolbox"
LOG_PREFIX="[cubesandbox-deployment-verify]"
if [[ -f "${SHARED_VERIFY}" && -f "${REPORT_SAFETY}" ]]; then
  exec bash "${SHARED_VERIFY}" "$@"
fi

REASON="blocked: missing shared implementation ${SHARED_VERIFY} or its safe report helper; reinstall cubesandbox-deployment alongside this skill"
printf '%s ERROR: %s\n' "${LOG_PREFIX}" "${REASON}" >&2
REPORT_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --report)
      if [[ $# -lt 2 ]]; then
        printf '%s ERROR: --report requires a JSON file path; cannot record blocked verdict\n' "${LOG_PREFIX}" >&2
        exit 4
      fi
      REPORT_FILE="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [[ -z "${REPORT_FILE}" ]]; then
  printf '%s ERROR: --report is required to record the blocked verdict safely\n' "${LOG_PREFIX}" >&2
  exit 4
fi

if [[ -f "${REPORT_SAFETY}" ]]; then
  if python3 "${REPORT_SAFETY}" blocked "${REPORT_FILE}" cubesandbox-deployment-verify verify \
    --install-root "${INSTALL_ROOT}" --reason "${REASON}"; then
    printf '%s JSON report: %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  else
    printf '%s ERROR: could not safely write blocked JSON report %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
  fi
  exit 4
fi

# Last-resort path when the whole shared Skill is absent. Keep the same
# real-path, existing-report identity, and atomic 0600 protections instead of
# leaving a stale success report behind.
if python3 - "${REPORT_FILE}" "${INSTALL_ROOT}" "${REASON}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
install_root = Path(os.path.realpath(os.path.abspath(sys.argv[2])))
reason = sys.argv[3]
resolved = Path(os.path.realpath(os.path.abspath(str(path))))
if not path.parent.is_dir():
    raise SystemExit("report parent directory does not exist")
if path.is_symlink() or (path.exists() and not path.is_file()):
    raise SystemExit("report path must be a regular, non-symlink file")
try:
    resolved.relative_to(install_root)
except ValueError:
    pass
else:
    raise SystemExit("verification report must be outside INSTALL_ROOT")
if path.exists():
    try:
        with path.open("r", encoding="utf-8") as handle:
            old = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("refusing to overwrite an unrelated existing file: " + str(exc))
    if not isinstance(old, dict) or type(old.get("schema_version")) is not int \
            or old.get("schema_version") != 1 \
            or old.get("skill") != "cubesandbox-deployment-verify" or old.get("mode") != "verify":
        raise SystemExit("existing JSON report identity mismatch")
report = {
    "schema_version": 1,
    "skill": "cubesandbox-deployment-verify",
    "mode": "verify",
    "status": "blocked",
    "success": False,
    "quickcheck_exit_code": None,
    "checks": {
        "runtime_env": "blocked",
        "official_quickcheck": "blocked",
        "services_and_health_endpoints": "blocked",
    },
    "official_evidence": {
        "control_role": False,
        "systemd_units": False,
        "cube_api_health": False,
        "cube_ops_health": False,
        "official_completion": False,
    },
    "reason": reason,
    "side_effects": {
        "release_discovery": False,
        "bundle_downloaded": False,
        "installation_or_restart": False,
        "host_configuration_changed": False,
    },
}
descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
try:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise SystemExit("report path changed before replacement")
    os.replace(temporary, str(path))
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
then
  printf '%s JSON report: %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
else
  printf '%s ERROR: could not safely write blocked JSON report %s\n' "${LOG_PREFIX}" "${REPORT_FILE}" >&2
fi
exit 4
