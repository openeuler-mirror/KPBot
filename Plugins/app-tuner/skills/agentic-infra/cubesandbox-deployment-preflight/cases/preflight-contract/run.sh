#!/usr/bin/env bash
# Tier-A: real preflight wrapper + patched shared deploy against a fake host.
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
python3 "${SHARED_SKILL_DIR}/cases/lib/deployment_fixture.py" "${AGENTIC_DIR}" "${TMP_DIR}/fixture" nested >"${TMP_DIR}/fixture.json"
FIXTURE="${TMP_DIR}/fixture"
LAYOUT="${FIXTURE}/agentic-infra"
HOST="${FIXTURE}/host"
export PATH="${FIXTURE}/bin:${PATH}"
export TMPDIR="${FIXTURE}/run-tmp"
export FAKE_BUNDLE="${FIXTURE}/cube-sandbox-one-click-v1.2.3-amd64.tar.gz"
export FAKE_CURL_LOG="${TMP_DIR}/curl.log"
export FAKE_SYSTEMCTL_LOG="${TMP_DIR}/systemctl.log"
export FAKE_QUICKCHECK_LOG="${TMP_DIR}/quickcheck.log"
export FAKE_INSTALL_RECORD="${TMP_DIR}/installer.record"
export FAKE_FORBIDDEN_LOG="${TMP_DIR}/forbidden.log"
snapshot_host() {
  python3 - "${HOST}" <<'PY'
import hashlib
from pathlib import Path
import sys
root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    digest.update(str(path.relative_to(root)).encode())
    digest.update(b"dir" if path.is_dir() else path.read_bytes())
print(digest.hexdigest())
PY
}
BEFORE="$(snapshot_host)"
ENV_BEFORE="$(sha256sum "${FIXTURE}/deployment.env" | awk '{print $1}')"
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" \
  --env "${FIXTURE}/deployment.env" --report "${FIXTURE}/deployment.env" \
  >"${TMP_DIR}/same-env-report.log" 2>&1
SAME_ENV_RC=$?
ENV_AFTER="$(sha256sum "${FIXTURE}/deployment.env" | awk '{print $1}')"
printf 'unrelated sentinel; not JSON\n' >"${TMP_DIR}/unrelated.json"
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" \
  --env "${FIXTURE}/deployment.env" --report "${TMP_DIR}/unrelated.json" \
  >"${TMP_DIR}/unrelated-report.log" 2>&1
UNRELATED_RC=$?
printf '{"schema_version":1,"skill":"other-skill","mode":"preflight"}\n' >"${TMP_DIR}/wrong-identity.json"
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" \
  --env "${FIXTURE}/deployment.env" --report "${TMP_DIR}/wrong-identity.json" \
  >"${TMP_DIR}/wrong-identity-report.log" 2>&1
WRONG_IDENTITY_RC=$?
REPORT_SAFETY_OK=1
[[ "${SAME_ENV_RC}" -eq 4 && "${UNRELATED_RC}" -eq 4 && "${WRONG_IDENTITY_RC}" -eq 4 && "${ENV_BEFORE}" == "${ENV_AFTER}" ]] || REPORT_SAFETY_OK=0
[[ "$(cat "${TMP_DIR}/unrelated.json")" == "unrelated sentinel; not JSON" ]] || REPORT_SAFETY_OK=0
grep -Fq '"skill":"other-skill"' "${TMP_DIR}/wrong-identity.json" || REPORT_SAFETY_OK=0
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" --env "${FIXTURE}/deployment.env" --report "${RESULTS_DIR}/preflight-success.json" >"${TMP_DIR}/success.log" 2>&1
SUCCESS_RC=$?
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" --env "${FIXTURE}/deployment-no-sha.env" --report "${RESULTS_DIR}/preflight-no-sha.json" >"${TMP_DIR}/no-sha.log" 2>&1
NO_SHA_RC=$?
printf '%s\n' 'CUBE_VERSION=latest' 'MIRROR=' 'CUBE_PVM_ENABLE=0' \
  'CUBE_SANDBOX_NETWORK_CIDR=10.100.0.0/18' 'CUBE_SANDBOX_NODE_IP=192.0.2.10' \
  >"${FIXTURE}/deployment-api-fallback.env"
chmod 600 "${FIXTURE}/deployment-api-fallback.env"
mv "${LAYOUT}/cubesandbox-deployment/scripts/discover_release.py" \
  "${LAYOUT}/cubesandbox-deployment/scripts/discover_release.py.real"
cat >"${LAYOUT}/cubesandbox-deployment/scripts/discover_release.py" <<'PY'
#!/usr/bin/env python3
# Simulate an official API fallback record with no trusted digest; no network.
print("v1.2.3\x1ecube-sandbox-one-click-v1.2.3-amd64.tar.gz\x1ehttps://github.com/TencentCloud/CubeSandbox/releases/download/v1.2.3/cube-sandbox-one-click-v1.2.3-amd64.tar.gz\x1e\x1e")
PY
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" \
  --env "${FIXTURE}/deployment-api-fallback.env" --report "${RESULTS_DIR}/preflight-api-no-sha.json" \
  >"${TMP_DIR}/api-no-sha.log" 2>&1
API_NO_SHA_RC=$?
mv "${LAYOUT}/cubesandbox-deployment/scripts/discover_release.py.real" \
  "${LAYOUT}/cubesandbox-deployment/scripts/discover_release.py"
FAKE_GLOBAL_IP_COUNT=2 bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" --env "${FIXTURE}/deployment-no-node.env" --report "${RESULTS_DIR}/preflight-failure.json" >"${TMP_DIR}/failure.log" 2>&1
FAILURE_RC=$?
mv "${LAYOUT}/cubesandbox-deployment/scripts/deploy.sh" "${LAYOUT}/cubesandbox-deployment/scripts/deploy.sh.disabled"
bash "${LAYOUT}/cubesandbox-deployment-preflight/scripts/run.sh" --report "${RESULTS_DIR}/preflight-blocked.json" >"${TMP_DIR}/blocked.log" 2>&1
BLOCKED_RC=$?
AFTER="$(snapshot_host)"
read -r PREFLIGHT_OK MISSING_SHA_REFUSED FAILURE_DETECTED MISSING_SHARED_BLOCKED SECRET_SAFE < <(python3 - \
  "${RESULTS_DIR}/preflight-success.json" "${RESULTS_DIR}/preflight-no-sha.json" \
  "${RESULTS_DIR}/preflight-api-no-sha.json" "${RESULTS_DIR}/preflight-failure.json" \
  "${RESULTS_DIR}/preflight-blocked.json" "${SUCCESS_RC}" "${NO_SHA_RC}" \
  "${API_NO_SHA_RC}" "${FAILURE_RC}" "${BLOCKED_RC}" <<'PY'
import json
from pathlib import Path
import sys
success = json.loads(Path(sys.argv[1]).read_text())
no_sha = json.loads(Path(sys.argv[2]).read_text())
api_no_sha = json.loads(Path(sys.argv[3]).read_text())
failure = json.loads(Path(sys.argv[4]).read_text())
blocked = json.loads(Path(sys.argv[5]).read_text())
ok = (sys.argv[6] == "0" and success["status"] == "success" and success["success"] and success["ready_for_install"] and success["sha256_pinned"] and success["release"]["version"] == "v1.2.3" and success["host"]["release_arch"] == "amd64" and not any(success["side_effects"].values()))
custom_digest_refused = (sys.argv[7] == "1" and no_sha["status"] == "failed" and not no_sha["success"] and not no_sha["ready_for_install"] and not no_sha["sha256_pinned"] and any("trusted SHA-256" in issue for issue in no_sha["issues"]))
api_digest_refused = (sys.argv[8] == "1" and api_no_sha["status"] == "failed" and not api_no_sha["success"] and not api_no_sha["ready_for_install"] and not api_no_sha["sha256_pinned"] and any("API fallback" in issue and "trusted SHA-256" in issue for issue in api_no_sha["issues"]))
digest_refused = custom_digest_refused and api_digest_refused
failed = (sys.argv[9] == "1" and failure["status"] == "failed" and not failure["success"] and not failure["ready_for_install"] and any("multiple host IPv4" in issue for issue in failure["issues"]))
is_blocked = (sys.argv[10] == "4" and blocked["status"] == "blocked" and not blocked["success"] and not blocked["sha256_pinned"] and "missing shared implementation" in blocked["reason"])
combined = "".join(Path(path).read_text(errors="replace") for path in sys.argv[1:6])
secret_safe = "fake-token" not in combined
print(int(ok), int(digest_refused), int(failed), int(is_blocked), int(secret_safe))
PY
)
[[ "$(/usr/bin/stat -c %a "${RESULTS_DIR}/preflight-success.json")" == "600" ]] || PREFLIGHT_OK=0
if grep -R -Fq 'fake-token' "${TMP_DIR}/success.log" "${TMP_DIR}/no-sha.log" "${TMP_DIR}/api-no-sha.log" "${TMP_DIR}/failure.log" "${TMP_DIR}/blocked.log"; then SECRET_SAFE=0; fi
NO_SIDE_EFFECTS=1
[[ "${BEFORE}" == "${AFTER}" ]] || NO_SIDE_EFFECTS=0
for artifact in "${FAKE_CURL_LOG}" "${FAKE_SYSTEMCTL_LOG}" "${FAKE_QUICKCHECK_LOG}" "${FAKE_INSTALL_RECORD}" "${FAKE_FORBIDDEN_LOG}"; do [[ ! -e "${artifact}" ]] || NO_SIDE_EFFECTS=0; done
FAILURES=0
record() {
  local description="$1" condition="$2"
  if [[ "${condition}" -eq 1 ]]; then printf '[case] ok: %s\n' "${description}"; else printf '[case] FAIL: %s\n' "${description}" >&2; FAILURES=$((FAILURES + 1)); fi
}
record "successful preflight emits machine-readable ready and digest-pinned verdict" "${PREFLIGHT_OK}"
record "API fallback and custom URL without trusted SHA-256 never download" "${MISSING_SHA_REFUSED}"
record "multi-IP failure is structured and not ready" "${FAILURE_DETECTED}"
record "missing shared implementation is blocked" "${MISSING_SHARED_BLOCKED}"
record "report cannot overwrite env input or an unrelated sentinel" "${REPORT_SAFETY_OK}"
record "preflight leaves fake host and protected commands untouched" "${NO_SIDE_EFFECTS}"
record "credentials are redacted from logs and reports" "${SECRET_SAFE}"
SUCCESS=false
[[ "${FAILURES}" -eq 0 ]] && SUCCESS=true
python3 - "${RESULTS_DIR}/result.json" "${SUCCESS}" "${PREFLIGHT_OK}" "${MISSING_SHA_REFUSED}" "${FAILURE_DETECTED}" "${MISSING_SHARED_BLOCKED}" "${REPORT_SAFETY_OK}" "${NO_SIDE_EFFECTS}" "${SECRET_SAFE}" <<'PY'
import json
import sys
path, success, preflight, no_sha, failure, blocked, report_safe, side_effects, secret_safe = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"success": success == "true", "preflight_success": preflight == "1", "missing_sha_refused": no_sha == "1", "failure_detected": failure == "1", "missing_shared_blocked": blocked == "1", "report_path_safe": report_safe == "1", "no_side_effects": side_effects == "1", "secret_safe": secret_safe == "1"}, handle, indent=2)
    handle.write("\n")
PY
cat "${RESULTS_DIR}/result.json"
[[ "${SUCCESS}" == "true" ]]
