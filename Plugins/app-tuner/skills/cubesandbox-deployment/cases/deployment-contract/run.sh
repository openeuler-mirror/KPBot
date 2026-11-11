#!/usr/bin/env bash
# Side-effect-free contract checks for the deployment helper and shell entrypoint.
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "${CASE_DIR}/../.." && pwd)"
RESULTS_DIR="${CASE_DIR}/results"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${RESULTS_DIR}"

STATIC_RAN=0
STATIC_FAILURES=0
RELEASE_RAN=0
RELEASE_FAILURES=0

record_static() {
  local description="$1" rc="$2"
  STATIC_RAN=$((STATIC_RAN + 1))
  if [[ "${rc}" -eq 0 ]]; then
    printf '[case] ok: %s\n' "${description}"
  else
    printf '[case] FAIL: %s\n' "${description}" >&2
    STATIC_FAILURES=$((STATIC_FAILURES + 1))
  fi
}

bash -n "${SKILL_DIR}/scripts/deploy.sh"
record_static "deploy.sh bash syntax" "$?"

bash "${SKILL_DIR}/scripts/deploy.sh" --help >"${TMP_DIR}/help.txt" 2>&1
rc=$?
grep -q -- '--preflight-only' "${TMP_DIR}/help.txt" && ! grep -q -- '--upgrade' "${TMP_DIR}/help.txt"
record_static "help documents read-only preflight and fresh-install-only scope" "$((rc != 0 || $? != 0))"

PYTHONPYCACHEPREFIX="${TMP_DIR}/pycache" python3 -m py_compile "${SKILL_DIR}/scripts/discover_release.py"
record_static "discover_release.py syntax" "$?"

grep -q 'ONE_CLICK_RUN_QUICKCHECK=1' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'EXPECTED_SHA256' "${SKILL_DIR}/scripts/deploy.sh" \
  && ! grep -q 'ONE_CLICK_SKIP_PRECHECK=1' "${SKILL_DIR}/scripts/deploy.sh"
record_static "quickcheck, integrity verification, and no precheck bypass" "$?"

record=$'v1.2.3\x1easset.tar.gz\x1ehttps://github.com/asset.tar.gz\x1e\x1eabcdef'
IFS=$'\x1e' read -r parsed_version parsed_asset parsed_url parsed_fallback parsed_sha <<<"${record}"
[[ "${parsed_version}" == "v1.2.3" && -z "${parsed_fallback}" && "${parsed_sha}" == "abcdef" ]]
record_static "release protocol preserves an empty fallback and the SHA field" "$?"

CUBE_SANDBOX_INSTALL_ROOT=/tmp/nonstandard-cube-root \
  bash "${SKILL_DIR}/scripts/deploy.sh" --preflight-only >"${TMP_DIR}/custom-root.log" 2>&1
rc=$?
[[ "${rc}" -ne 0 ]] && grep -q 'custom CUBE_SANDBOX_INSTALL_ROOT is unsupported' "${TMP_DIR}/custom-root.log"
record_static "nondefault install roots fail before host changes" "$?"

grep -Fq 'export ONE_CLICK_ENV_FILE="${UPSTREAM_ENV_FILE}"' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'flock -n 9' "${SKILL_DIR}/scripts/deploy.sh" \
  && [[ "$(grep -c 'validate_install_root final' "${SKILL_DIR}/scripts/deploy.sh")" -eq 2 ]] \
  && grep -q 'partial|unknown' "${SKILL_DIR}/scripts/deploy.sh"
record_static "upstream dotenv, deployment lock, rechecks, and partial-install guard" "$?"

grep -q 'check_sandbox_cidr' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'check_listening_ports' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'NODE_IP_EXPLICIT' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'CUBE_API_HEALTH_ADDR' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'CUBE_OPS_UPSTREAM' "${SKILL_DIR}/scripts/deploy.sh" \
  && grep -q 'curl-config' "${SKILL_DIR}/scripts/deploy.sh"
record_static "CIDR, ports, explicit node IP, and credential-safe curl contracts" "$?"

PYTHONPYCACHEPREFIX="${TMP_DIR}/pycache" python3 - "${SKILL_DIR}/scripts/discover_release.py" >"${TMP_DIR}/release-results.json" <<'PY'
import importlib.util
import json
import sys

sys.dont_write_bytecode = True
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("discover_release", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

results = []


def record(description, condition):
    results.append({"description": description, "success": bool(condition)})


def asset(arch, digest="a" * 64):
    name = "cube-sandbox-one-click-v1.2.3-" + arch + ".tar.gz"
    return {
        "name": name,
        "digest": "sha256:" + digest,
        "browser_download_url": (
            "https://github.com/TencentCloud/CubeSandbox/releases/download/v1.2.3/" + name
        ),
    }


release = {"tag_name": "v1.2.3", "assets": [asset("amd64"), asset("arm64", "b" * 64)]}

amd64 = module.resolve_release(release, "amd64", "github")
record("x86 selects the exact amd64 asset", amd64["asset_name"].endswith("-amd64.tar.gz"))
record("GitHub mode keeps the official URL and digest",
       amd64["download_url"] == amd64["github_url"]
       and amd64["sha256"] == "a" * 64
       and amd64["fallback_url"] == "")

arm64 = module.resolve_release(release, "arm64", "github")
record("ARM selects the exact arm64 asset", arm64["asset_name"].endswith("-arm64.tar.gz"))
record("ARM preserves its distinct digest", arm64["sha256"] == "b" * 64)

cn = module.resolve_release(release, "arm64", "cn")
record("CN mode supports ARM and retains GitHub fallback",
       cn["download_url"].startswith(module.CNB_DOWNLOAD_PREFIX)
       and cn["download_url"].endswith("-arm64.tar.gz")
       and cn["fallback_url"] == cn["github_url"])

try:
    module.select_asset({"tag_name": "v1.2.3", "assets": [asset("amd64")]}, "arm64")
    missing_arch_rejected = False
except module.ReleaseError as exc:
    missing_arch_rejected = "arm64" in str(exc)
record("missing target architecture fails closed", missing_arch_rejected)

bad_digest_release = {"tag_name": "v1.2.3", "assets": [asset("amd64", "not-a-digest")]}
try:
    module.select_asset(bad_digest_release, "amd64")
    bad_digest_rejected = False
except module.ReleaseError as exc:
    bad_digest_rejected = "digest" in str(exc)
record("missing or malformed digest fails closed", bad_digest_rejected)

record("numeric versions normalize to release tags",
       module.release_endpoint(module.GITHUB_API_BASE, "1.2.3").endswith("/releases/tags/v1.2.3"))

cn_latest = {"url": cn["download_url"].replace("-arm64.tar.gz", "-amd64.tar.gz")}
fallback_asset = module.asset_from_cn_latest(cn_latest, "arm64")
record("official CN latest metadata selects the matching ARM asset",
       fallback_asset["version"] == "v1.2.3"
       and fallback_asset["asset_name"].endswith("-arm64.tar.gz"))

original_fetch_release = module.fetch_release
original_fetch_json = module.fetch_json
try:
    def rate_limited(*args, **kwargs):
        raise module.ReleaseError("HTTP Error 403: rate limit exceeded")

    module.fetch_release = rate_limited
    module.fetch_json = lambda url, timeout: cn_latest
    fallback = module.discover(module.GITHUB_API_BASE, "latest", "arm64", "cn", 1)
    fallback_works = (fallback["asset_name"].endswith("-arm64.tar.gz")
                      and fallback["download_url"].startswith(module.CNB_DOWNLOAD_PREFIX)
                      and fallback["sha256"] == "")
finally:
    module.fetch_release = original_fetch_release
    module.fetch_json = original_fetch_json
record("API rate limit fails over only to official URLs with a digest warning", fallback_works)

print(json.dumps(results))
PY
rc=$?
if [[ "${rc}" -eq 0 ]]; then
  while IFS= read -r line; do
    description="${line%|*}"
    result="${line##*|}"
    RELEASE_RAN=$((RELEASE_RAN + 1))
    if [[ "${result}" == "ok" ]]; then
      printf '[case] ok: %s\n' "${description}"
    else
      printf '[case] FAIL: %s\n' "${description}" >&2
      RELEASE_FAILURES=$((RELEASE_FAILURES + 1))
    fi
  done < <(python3 - "${TMP_DIR}/release-results.json" <<'PY'
import json
import sys
for item in json.load(open(sys.argv[1], encoding="utf-8")):
    print(item["description"] + "|" + ("ok" if item["success"] else "failed"))
PY
  )
else
  RELEASE_RAN=$((RELEASE_RAN + 1))
  RELEASE_FAILURES=$((RELEASE_FAILURES + 1))
  printf '[case] FAIL: release helper contract program\n' >&2
fi

SUCCESS=false
if [[ "${STATIC_FAILURES}" -eq 0 && "${RELEASE_FAILURES}" -eq 0 ]]; then
  SUCCESS=true
fi

python3 - "${RESULTS_DIR}/result.json" "${SUCCESS}" "${STATIC_RAN}" "${STATIC_FAILURES}" \
  "${RELEASE_RAN}" "${RELEASE_FAILURES}" <<'PY'
import json
import sys
path, success, static_ran, static_failures, release_ran, release_failures = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "success": success == "true",
        "static_ran": int(static_ran),
        "static_failures": int(static_failures),
        "release_ran": int(release_ran),
        "release_failures": int(release_failures),
    }, handle, indent=2)
    handle.write("\n")
PY

cat "${RESULTS_DIR}/result.json"
[[ "${SUCCESS}" == "true" ]]
