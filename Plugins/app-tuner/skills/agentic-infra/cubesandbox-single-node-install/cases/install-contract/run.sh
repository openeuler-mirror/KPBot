#!/usr/bin/env bash
# Tier-A: authorized install routing and the real shared installer with fakes.
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
RUNNER="${LAYOUT}/cubesandbox-single-node-install/scripts/run.sh"
export PATH="${FIXTURE}/bin:${PATH}"
export TMPDIR="${FIXTURE}/run-tmp"
export FAKE_BUNDLE="${FIXTURE}/cube-sandbox-one-click-v1.2.3-amd64.tar.gz"
export FAKE_CURL_LOG="${TMP_DIR}/curl.log"
export FAKE_SYSTEMCTL_LOG="${TMP_DIR}/systemctl.log"
export FAKE_QUICKCHECK_LOG="${TMP_DIR}/quickcheck.log"
export FAKE_INSTALL_RECORD="${TMP_DIR}/installer.record"
export FAKE_FORBIDDEN_LOG="${TMP_DIR}/forbidden.log"
bash "${RUNNER}" --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/unauthorized.log" 2>&1
UNAUTHORIZED_RC=$?
bash "${RUNNER}" --confirm-fresh-install --preflight-only >"${TMP_DIR}/mode-bypass.log" 2>&1
MODE_BYPASS_RC=$?
ONE_CLICK_SKIP_PRECHECK=1 bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 >"${TMP_DIR}/safety-bypass.log" 2>&1
SAFETY_BYPASS_RC=$?
AUTH_REFUSED=1
[[ "${UNAUTHORIZED_RC}" -eq 2 && "${MODE_BYPASS_RC}" -eq 2 && "${SAFETY_BYPASS_RC}" -eq 2 ]] || AUTH_REFUSED=0
[[ ! -e "${FAKE_INSTALL_RECORD}" ]] || AUTH_REFUSED=0
bash "${RUNNER}" --confirm-fresh-install --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/missing-version.log" 2>&1
MISSING_VERSION_RC=$?
EXPECT_VERSION_REQUIRED=1
[[ "${MISSING_VERSION_RC}" -eq 2 && ! -e "${FAKE_INSTALL_RECORD}" ]] && grep -q -- '--expect-version is required' "${TMP_DIR}/missing-version.log" || EXPECT_VERSION_REQUIRED=0
mkdir -p "${INSTALL_ROOT}"
printf 'ONE_CLICK_DEPLOY_ROLE=control\n' >"${INSTALL_ROOT}/.one-click.env"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/existing.log" 2>&1
EXISTING_RC=$?
EXISTING_REFUSED=1
[[ "${EXISTING_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" ]] && grep -q 'already exists' "${TMP_DIR}/existing.log" || EXISTING_REFUSED=0
rm -rf "${INSTALL_ROOT}"
mkdir -p "${INSTALL_ROOT}/CubeAPI"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/partial.log" 2>&1
PARTIAL_RC=$?
PARTIAL_REFUSED=1
[[ "${PARTIAL_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" ]] && grep -q 'partial or unrecognized' "${TMP_DIR}/partial.log" || PARTIAL_REFUSED=0
rm -rf "${INSTALL_ROOT}"
mkdir -p "${INSTALL_ROOT}"
printf 'unknown\n' >"${INSTALL_ROOT}/unknown.file"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/unknown.log" 2>&1
UNKNOWN_RC=$?
UNKNOWN_REFUSED=1
[[ "${UNKNOWN_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" ]] && grep -q 'partial or unrecognized' "${TMP_DIR}/unknown.log" || UNKNOWN_REFUSED=0
rm -rf "${INSTALL_ROOT}"
rm -f "${FAKE_CURL_LOG}"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment-no-sha.env" >"${TMP_DIR}/no-sha.log" 2>&1
NO_SHA_RC=$?
NO_SHA_REFUSED=1
[[ "${NO_SHA_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" && ! -e "${FAKE_CURL_LOG}" ]] && grep -q 'trusted SHA-256 digest is required' "${TMP_DIR}/no-sha.log" || NO_SHA_REFUSED=0
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment-bad-sha.env" >"${TMP_DIR}/bad-sha.log" 2>&1
BAD_SHA_RC=$?
SHA_REFUSED=1
[[ "${BAD_SHA_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" && ! -e "${INSTALL_ROOT}/.one-click.env" ]] && grep -q 'SHA-256 mismatch' "${TMP_DIR}/bad-sha.log" || SHA_REFUSED=0
rm -f "${FAKE_CURL_LOG}" "${FAKE_INSTALL_RECORD}" "${FAKE_QUICKCHECK_LOG}" "${FAKE_SYSTEMCTL_LOG}"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment-drift.env" >"${TMP_DIR}/drift.log" 2>&1
DRIFT_RC=$?
VERSION_DRIFT_REFUSED=1
[[ "${DRIFT_RC}" -ne 0 && ! -e "${FAKE_INSTALL_RECORD}" && ! -e "${FAKE_CURL_LOG}" && ! -e "${FAKE_QUICKCHECK_LOG}" ]] && grep -q 'differs from preflight release.version v1.2.3' "${TMP_DIR}/drift.log" && grep -q 'rerun preflight' "${TMP_DIR}/drift.log" || VERSION_DRIFT_REFUSED=0
rm -rf "${INSTALL_ROOT}"
rm -f "${FAKE_CURL_LOG}"
bash "${RUNNER}" --confirm-fresh-install --expect-version v1.2.3 --env "${FIXTURE}/deployment.env" >"${TMP_DIR}/success.log" 2>&1
SUCCESS_RC=$?
INSTALL_OK=1
[[ "${SUCCESS_RC}" -eq 0 && -f "${INSTALL_ROOT}/.one-click.env" && -s "${FAKE_INSTALL_RECORD}" ]] || INSTALL_OK=0
grep -q '^mode=install$' "${FAKE_INSTALL_RECORD}" 2>/dev/null || INSTALL_OK=0
grep -q '^quickcheck=1$' "${FAKE_INSTALL_RECORD}" 2>/dev/null || INSTALL_OK=0
grep -q '^role=control$' "${FAKE_INSTALL_RECORD}" 2>/dev/null || INSTALL_OK=0
QUICKCHECK_OK=1
[[ -s "${FAKE_QUICKCHECK_LOG}" && -s "${FAKE_SYSTEMCTL_LOG}" && -s "${FAKE_CURL_LOG}" ]] && grep -q '\[quickcheck\] OK' "${TMP_DIR}/success.log" || QUICKCHECK_OK=0
SECRET_SAFE=1
if grep -R -Fq -e 'fake-token' -e 'verify-secret-do-not-log' "${TMP_DIR}/success.log" "${FAKE_INSTALL_RECORD}" "${RESULTS_DIR}"; then SECRET_SAFE=0; fi
NO_FORBIDDEN_ACTIONS=1
[[ ! -e "${FAKE_FORBIDDEN_LOG}" ]] || NO_FORBIDDEN_ACTIONS=0
FAILURES=0
record() {
  local description="$1" condition="$2"
  if [[ "${condition}" -eq 1 ]]; then printf '[case] ok: %s\n' "${description}"; else printf '[case] FAIL: %s\n' "${description}" >&2; FAILURES=$((FAILURES + 1)); fi
}
record "authorization, mode conversion, and safety bypass are refused" "${AUTH_REFUSED}"
record "approved preflight release.version is mandatory" "${EXPECT_VERSION_REQUIRED}"
record "complete existing installation is refused" "${EXISTING_REFUSED}"
record "partial installation is refused" "${PARTIAL_REFUSED}"
record "unknown installation content is refused" "${UNKNOWN_REFUSED}"
record "missing trusted SHA-256 prevents download and installation" "${NO_SHA_REFUSED}"
record "SHA mismatch prevents official installer execution" "${SHA_REFUSED}"
record "v1.2.3 to v1.2.4 drift stops before download or installation" "${VERSION_DRIFT_REFUSED}"
record "fresh all-in-one install succeeds through shared deploy.sh" "${INSTALL_OK}"
record "official installer executes service-health quickcheck" "${QUICKCHECK_OK}"
record "no bpffs/PVM side effect occurs in the prepared fake host" "${NO_FORBIDDEN_ACTIONS}"
record "download credentials and runtime secrets stay out of output" "${SECRET_SAFE}"
SUCCESS=false
[[ "${FAILURES}" -eq 0 ]] && SUCCESS=true
python3 - "${RESULTS_DIR}/result.json" "${SUCCESS}" "${AUTH_REFUSED}" "${EXPECT_VERSION_REQUIRED}" "${EXISTING_REFUSED}" "${PARTIAL_REFUSED}" "${UNKNOWN_REFUSED}" "${NO_SHA_REFUSED}" "${SHA_REFUSED}" "${VERSION_DRIFT_REFUSED}" "${INSTALL_OK}" "${QUICKCHECK_OK}" "${NO_FORBIDDEN_ACTIONS}" "${SECRET_SAFE}" <<'PY'
import json
import sys
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({
        "success": sys.argv[2] == "true",
        "bypass_refused": sys.argv[3] == "1",
        "expect_version_required": sys.argv[4] == "1",
        "existing_refused": sys.argv[5] == "1",
        "partial_refused": sys.argv[6] == "1",
        "unknown_refused": sys.argv[7] == "1",
        "missing_sha_refused": sys.argv[8] == "1",
        "sha_failure_refused": sys.argv[9] == "1",
        "version_drift_refused": sys.argv[10] == "1",
        "fresh_install_success": sys.argv[11] == "1",
        "official_quickcheck_ran": sys.argv[12] == "1",
        "no_forbidden_actions": sys.argv[13] == "1",
        "secret_safe": sys.argv[14] == "1",
    }, handle, indent=2)
    handle.write("\n")
PY
cat "${RESULTS_DIR}/result.json"
[[ "${SUCCESS}" == "true" ]]
