#!/usr/bin/env bash
# Tier-A: independent official quickcheck success/failure/blocked contracts.
set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "${CASE_DIR}/../.." && pwd)"
AGENTIC_DIR="$(cd "${SKILL_DIR}/.." && pwd)"
SHARED_SKILL_DIR="${AGENTIC_DIR}/cubesandbox-deployment"
RESULTS_DIR="${CASE_DIR}/results"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${RESULTS_DIR}"
export PYTHONDONTWRITEBYTECODE=1
python3 "${SHARED_SKILL_DIR}/cases/lib/deployment_fixture.py" "${AGENTIC_DIR}" "${TMP_DIR}/fixture" flat >"${TMP_DIR}/fixture.json"
FIXTURE="${TMP_DIR}/fixture"
LAYOUT="${FIXTURE}/skills"
INSTALL_ROOT="${FIXTURE}/host/usr/local/services/cubetoolbox"
QUICKCHECK_DIR="${INSTALL_ROOT}/scripts/one-click"
RUNTIME_DIR="${FIXTURE}/host/var/run/cube-sandbox-one-click"
LOG_DIR="${FIXTURE}/host/var/log/cube-sandbox-one-click"
RUNNER="${LAYOUT}/cubesandbox-deployment-verify/scripts/run.sh"
SHARED_VERIFY="${LAYOUT}/cubesandbox-deployment/scripts/verify.sh"
mkdir -p "${QUICKCHECK_DIR}"
cp "${FIXTURE}/bundle-src/cube-sandbox-one-click-v1.2.3-amd64/scripts/one-click/quickcheck.sh" "${QUICKCHECK_DIR}/quickcheck.sh"
cp "${FIXTURE}/bundle-src/cube-sandbox-one-click-v1.2.3-amd64/scripts/one-click/common.sh" "${QUICKCHECK_DIR}/common.sh"
printf 'ONE_CLICK_DEPLOY_ROLE=control\nVERIFY_SECRET=verify-secret-do-not-log\n' >"${INSTALL_ROOT}/.one-click.env"
chmod 600 "${INSTALL_ROOT}/.one-click.env"
printf 'ONE_CLICK_DEPLOY_ROLE=wrong-maintenance-session\n' >"${TMP_DIR}/maintenance-wrong.env"
chmod 600 "${TMP_DIR}/maintenance-wrong.env"
ENV_BEFORE="$(sha256sum "${INSTALL_ROOT}/.one-click.env" | awk '{print $1}')"
export PATH="${FIXTURE}/bin:${PATH}"
export TMPDIR="${FIXTURE}/run-tmp"
export FAKE_BUNDLE="${FIXTURE}/cube-sandbox-one-click-v1.2.3-amd64.tar.gz"
export FAKE_CURL_LOG="${TMP_DIR}/curl.log"
export FAKE_SYSTEMCTL_LOG="${TMP_DIR}/systemctl.log"
export FAKE_QUICKCHECK_LOG="${TMP_DIR}/quickcheck.log"
export FAKE_INSTALL_RECORD="${TMP_DIR}/installer.record"
export FAKE_FORBIDDEN_LOG="${TMP_DIR}/forbidden.log"

bash "${RUNNER}" --report "${INSTALL_ROOT}/.one-click.env" >"${TMP_DIR}/env-report.log" 2>&1
ENV_REPORT_RC=$?
bash "${RUNNER}" --report "${INSTALL_ROOT}/new-report.json" >"${TMP_DIR}/asset-report.log" 2>&1
ASSET_REPORT_RC=$?
printf 'verify sentinel; not JSON\n' >"${TMP_DIR}/unrelated.json"
bash "${RUNNER}" --report "${TMP_DIR}/unrelated.json" >"${TMP_DIR}/unrelated-report.log" 2>&1
UNRELATED_REPORT_RC=$?
printf '{"schema_version":1,"skill":"cubesandbox-deployment-preflight","mode":"preflight"}\n' >"${TMP_DIR}/wrong-identity.json"
bash "${RUNNER}" --report "${TMP_DIR}/wrong-identity.json" >"${TMP_DIR}/wrong-identity-report.log" 2>&1
WRONG_IDENTITY_RC=$?
REPORT_SAFETY_OK=1
[[ "${ENV_REPORT_RC}" -eq 4 && "${ASSET_REPORT_RC}" -eq 4 && "${UNRELATED_REPORT_RC}" -eq 4 && "${WRONG_IDENTITY_RC}" -eq 4 && ! -e "${INSTALL_ROOT}/new-report.json" ]] || REPORT_SAFETY_OK=0
[[ "$(cat "${TMP_DIR}/unrelated.json")" == "verify sentinel; not JSON" ]] || REPORT_SAFETY_OK=0
grep -Fq '"skill":"cubesandbox-deployment-preflight"' "${TMP_DIR}/wrong-identity.json" || REPORT_SAFETY_OK=0
[[ ! -e "${FAKE_QUICKCHECK_LOG}" ]] || REPORT_SAFETY_OK=0

ONE_CLICK_RUNTIME_ENV_FILE="${TMP_DIR}/maintenance-wrong.env" FAKE_REQUIRE_RUNTIME_ENV=1 FAKE_VERIFY_CANARY=1 \
  bash "${RUNNER}" --report "${RESULTS_DIR}/verify-success.json" >"${TMP_DIR}/success.log" 2>&1
SUCCESS_RC=$?
cp "${RESULTS_DIR}/verify-success.json" "${TMP_DIR}/verify-success-initial.json"
FAKE_VERIFY_CANARY=1 FAKE_HEALTHY=0 bash "${RUNNER}" --report "${RESULTS_DIR}/verify-health-failure.json" >"${TMP_DIR}/health-failure.log" 2>&1
HEALTH_FAILURE_RC=$?
mv "${QUICKCHECK_DIR}/quickcheck.sh" "${QUICKCHECK_DIR}/quickcheck.sh.disabled"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-missing-quickcheck.json" >"${TMP_DIR}/missing-quickcheck.log" 2>&1
MISSING_RC=$?
mv "${QUICKCHECK_DIR}/quickcheck.sh.disabled" "${QUICKCHECK_DIR}/quickcheck.sh"
printf '#!/usr/bin/env bash\necho "[quickcheck] OK"\nexit 0\n' >"${QUICKCHECK_DIR}/quickcheck.sh"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-incomplete.json" >"${TMP_DIR}/incomplete.log" 2>&1
INCOMPLETE_RC=$?
cp "${FIXTURE}/bundle-src/cube-sandbox-one-click-v1.2.3-amd64/scripts/one-click/quickcheck.sh" "${QUICKCHECK_DIR}/quickcheck.sh"

rm -f "${FAKE_QUICKCHECK_LOG}"
rmdir "${RUNTIME_DIR}"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-missing-runtime-dir.json" >"${TMP_DIR}/missing-runtime-dir.log" 2>&1
MISSING_RUNTIME_RC=$?
MISSING_RUNTIME_NOT_CREATED=1
[[ ! -e "${RUNTIME_DIR}" && ! -e "${FAKE_QUICKCHECK_LOG}" ]] || MISSING_RUNTIME_NOT_CREATED=0
mkdir -p "${RUNTIME_DIR}"
rmdir "${LOG_DIR}"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-missing-log-dir.json" >"${TMP_DIR}/missing-log-dir.log" 2>&1
MISSING_LOG_RC=$?
MISSING_LOG_NOT_CREATED=1
[[ ! -e "${LOG_DIR}" && ! -e "${FAKE_QUICKCHECK_LOG}" ]] || MISSING_LOG_NOT_CREATED=0
mkdir -p "${LOG_DIR}"

mv "${SHARED_VERIFY}" "${SHARED_VERIFY}.disabled"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-success.json" >"${TMP_DIR}/missing-shared.log" 2>&1
MISSING_SHARED_RC=$?
mv "${SHARED_VERIFY}.disabled" "${SHARED_VERIFY}"
cp "${TMP_DIR}/verify-success-initial.json" "${RESULTS_DIR}/verify-stale-success.json"
mv "${LAYOUT}/cubesandbox-deployment" "${LAYOUT}/cubesandbox-deployment.disabled"
bash "${RUNNER}" --report "${RESULTS_DIR}/verify-stale-success.json" >"${TMP_DIR}/missing-entire-shared.log" 2>&1
MISSING_ENTIRE_SHARED_RC=$?
mv "${LAYOUT}/cubesandbox-deployment.disabled" "${LAYOUT}/cubesandbox-deployment"
ENV_AFTER="$(sha256sum "${INSTALL_ROOT}/.one-click.env" | awk '{print $1}')"

read -r VERIFY_OK HEALTH_FAILED MISSING_BLOCKED INCOMPLETE_BLOCKED DIRECTORY_GUARD_OK MISSING_SHARED_BLOCKED < <(python3 - \
  "${TMP_DIR}/verify-success-initial.json" "${RESULTS_DIR}/verify-health-failure.json" \
  "${RESULTS_DIR}/verify-missing-quickcheck.json" "${RESULTS_DIR}/verify-incomplete.json" \
  "${RESULTS_DIR}/verify-missing-runtime-dir.json" "${RESULTS_DIR}/verify-missing-log-dir.json" \
  "${RESULTS_DIR}/verify-success.json" "${RESULTS_DIR}/verify-stale-success.json" \
  "${SUCCESS_RC}" "${HEALTH_FAILURE_RC}" "${MISSING_RC}" "${INCOMPLETE_RC}" \
  "${MISSING_RUNTIME_RC}" "${MISSING_LOG_RC}" "${MISSING_SHARED_RC}" "${MISSING_ENTIRE_SHARED_RC}" \
  "${MISSING_RUNTIME_NOT_CREATED}" "${MISSING_LOG_NOT_CREATED}" <<'PY'
import json
from pathlib import Path
import sys
success = json.loads(Path(sys.argv[1]).read_text())
health = json.loads(Path(sys.argv[2]).read_text())
missing = json.loads(Path(sys.argv[3]).read_text())
incomplete = json.loads(Path(sys.argv[4]).read_text())
missing_runtime = json.loads(Path(sys.argv[5]).read_text())
missing_log = json.loads(Path(sys.argv[6]).read_text())
missing_shared = json.loads(Path(sys.argv[7]).read_text())
missing_entire_shared = json.loads(Path(sys.argv[8]).read_text())
ok = (sys.argv[9] == "0" and success["status"] == "success" and success["success"] and all(value == "passed" for value in success["checks"].values()) and all(success["official_evidence"].values()) and not any(success["side_effects"].values()))
health_failed = (sys.argv[10] == "1" and health["status"] == "failed" and not health["success"] and health["checks"]["official_quickcheck"] == "failed" and health["checks"]["services_and_health_endpoints"] == "failed")
missing_blocked = (sys.argv[11] == "4" and missing["status"] == "blocked" and not missing["success"] and missing["quickcheck_exit_code"] is None)
incomplete_blocked = (sys.argv[12] == "4" and incomplete["status"] == "blocked" and not incomplete["success"] and incomplete["quickcheck_exit_code"] == 0)
directory_guard = (sys.argv[13] == "4" and sys.argv[14] == "4" and sys.argv[17:] == ["1", "1"] and all(report["status"] == "blocked" and not report["success"] and report["quickcheck_exit_code"] is None for report in (missing_runtime, missing_log)))
shared_blocked = (sys.argv[15] == "4" and sys.argv[16] == "4" and all(report["status"] == "blocked" and not report["success"] and "missing shared implementation" in report["reason"] for report in (missing_shared, missing_entire_shared)))
print(int(ok), int(health_failed), int(missing_blocked), int(incomplete_blocked), int(directory_guard), int(shared_blocked))
PY
)
[[ "$(/usr/bin/stat -c %a "${TMP_DIR}/verify-success-initial.json")" == "600" ]] || VERIFY_OK=0
READ_ONLY=1
[[ "${ENV_BEFORE}" == "${ENV_AFTER}" && ! -e "${FAKE_INSTALL_RECORD}" && ! -e "${FAKE_FORBIDDEN_LOG}" ]] || READ_ONLY=0
if grep -q -- '--config' "${FAKE_CURL_LOG}"; then READ_ONLY=0; fi
SECRET_SAFE=1
if grep -R -Fq -e 'verify-secret-do-not-log' -e 'fake-token' -e 'signed-query-secret' -e 'signed-user' -e '/private/egress' \
  "${RESULTS_DIR}" "${TMP_DIR}/success.log" "${TMP_DIR}/health-failure.log" \
  "${TMP_DIR}/missing-quickcheck.log" "${TMP_DIR}/incomplete.log" \
  "${TMP_DIR}/missing-runtime-dir.log" "${TMP_DIR}/missing-log-dir.log"; then SECRET_SAFE=0; fi
FAILURES=0
record() {
  local description="$1" condition="$2"
  if [[ "${condition}" -eq 1 ]]; then printf '[case] ok: %s\n' "${description}"; else printf '[case] FAIL: %s\n' "${description}" >&2; FAILURES=$((FAILURES + 1)); fi
}
record "official quickcheck uses pinned runtime env and complete health evidence" "${VERIFY_OK}"
record "health endpoint failure is not reported as success" "${HEALTH_FAILED}"
record "missing official quickcheck is blocked" "${MISSING_BLOCKED}"
record "zero exit without full official evidence is blocked" "${INCOMPLETE_BLOCKED}"
record "missing runtime/log directories block before quickcheck and are not created" "${DIRECTORY_GUARD_OK}"
record "missing shared verifier replaces stale success with blocked JSON" "${MISSING_SHARED_BLOCKED}"
record "report cannot overwrite runtime env, installation assets, or sentinels" "${REPORT_SAFETY_OK}"
record "verification performs no download/install/restart/config write" "${READ_ONLY}"
record "runtime secrets and signed URL components stay out of reports and logs" "${SECRET_SAFE}"
SUCCESS=false
[[ "${FAILURES}" -eq 0 ]] && SUCCESS=true
python3 - "${RESULTS_DIR}/result.json" "${SUCCESS}" "${VERIFY_OK}" "${HEALTH_FAILED}" "${MISSING_BLOCKED}" "${INCOMPLETE_BLOCKED}" "${DIRECTORY_GUARD_OK}" "${MISSING_SHARED_BLOCKED}" "${REPORT_SAFETY_OK}" "${READ_ONLY}" "${SECRET_SAFE}" <<'PY'
import json
import sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({
        "success": sys.argv[2] == "true",
        "verify_success": sys.argv[3] == "1",
        "health_failure_detected": sys.argv[4] == "1",
        "missing_quickcheck_blocked": sys.argv[5] == "1",
        "incomplete_evidence_blocked": sys.argv[6] == "1",
        "directory_guard": sys.argv[7] == "1",
        "missing_shared_blocked": sys.argv[8] == "1",
        "report_path_safe": sys.argv[9] == "1",
        "read_only": sys.argv[10] == "1",
        "secret_safe": sys.argv[11] == "1",
    }, handle, indent=2)
    handle.write("\n")
PY
cat "${RESULTS_DIR}/result.json"
[[ "${SUCCESS}" == "true" ]]
