#!/usr/bin/env python3
"""Build an isolated fake-host layout for CubeSandbox tier-A deployment cases."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile

SKILLS = (
    "cubesandbox-deployment",
    "cubesandbox-deployment-preflight",
    "cubesandbox-single-node-install",
    "cubesandbox-deployment-verify",
)


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    path.chmod(mode)


def main():
    if len(sys.argv) != 4 or sys.argv[3] not in ("nested", "flat"):
        raise SystemExit("usage: deployment_fixture.py <source-agentic-infra> <target> <nested|flat>")
    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    if target.exists():
        raise SystemExit("fixture target already exists: " + str(target))
    layout = target / ("agentic-infra" if sys.argv[3] == "nested" else "skills")
    layout.mkdir(parents=True)
    for name in SKILLS:
        source_skill = source / name
        if not (source_skill / "SKILL.md").is_file():
            raise SystemExit("missing source skill: " + str(source_skill))
        destination = layout / name
        destination.mkdir()
        shutil.copy2(source_skill / "SKILL.md", destination / "SKILL.md")
        shutil.copytree(source_skill / "scripts", destination / "scripts")

    host = target / "host"
    fake_bin = target / "bin"
    run_tmp = target / "run-tmp"
    fake_bin.mkdir()
    run_tmp.mkdir()
    install_root = host / "usr/local/services/cubetoolbox"
    systemctl = fake_bin / "systemctl"
    replacements = {
        "/usr/local/services/cubetoolbox": str(install_root),
        "/run/lock/cubesandbox-deployment.lock": str(host / "run/lock/cubesandbox-deployment.lock"),
        "/run/systemd/system": str(host / "run/systemd/system"),
        "/usr/bin/systemctl": str(systemctl),
        "/bin/systemctl": str(systemctl),
        "/proc/meminfo": str(host / "proc/meminfo"),
        "/proc/filesystems": str(host / "proc/filesystems"),
        "/proc/modules": str(host / "proc/modules"),
        "/sys/module/kvm_pvm": str(host / "sys/module/kvm_pvm"),
        "/sys/fs/cgroup": str(host / "sys/fs/cgroup"),
        "/sys/fs/bpf": str(host / "sys/fs/bpf"),
        "/data/cubelet": str(host / "data/cubelet"),
        "/dev/kvm": str(host / "dev/kvm"),
        "/etc/resolv.conf": str(host / "etc/resolv.conf"),
        "/etc/fstab": str(host / "etc/fstab"),
    }
    verify_only_replacements = {
        "/var/run/cube-sandbox-one-click": str(host / "var/run/cube-sandbox-one-click"),
        "/var/log/cube-sandbox-one-click": str(host / "var/log/cube-sandbox-one-click"),
    }
    all_replacements = dict(replacements)
    all_replacements.update(verify_only_replacements)
    pattern = re.compile("|".join(re.escape(item) for item in sorted(all_replacements, key=len, reverse=True)))
    for relative in ("scripts/deploy.sh", "scripts/verify.sh"):
        path = layout / "cubesandbox-deployment" / relative
        text = path.read_text(encoding="utf-8")
        required = replacements if relative == "scripts/deploy.sh" else {
            "/usr/local/services/cubetoolbox": replacements["/usr/local/services/cubetoolbox"],
            **verify_only_replacements,
        }
        for absolute_path in required:
            if absolute_path not in text:
                raise SystemExit("refusing to run an unpatched fake-host script; missing " + absolute_path)
        path.write_bytes(pattern.sub(lambda match: all_replacements[match.group(0)], text).encode("utf-8"))

    verify_wrapper = layout / "cubesandbox-deployment-verify/scripts/run.sh"
    wrapper_text = verify_wrapper.read_text(encoding="utf-8")
    if "/usr/local/services/cubetoolbox" not in wrapper_text:
        raise SystemExit("refusing to run an unpatched verify wrapper; missing install root")
    verify_wrapper.write_bytes(pattern.sub(
        lambda match: all_replacements[match.group(0)], wrapper_text
    ).encode("utf-8"))

    write(host / "proc/meminfo", "MemTotal:       16777216 kB\n")
    write(host / "proc/filesystems", "nodev\tsysfs\nnodev\tbpf\n\txfs\n")
    write(host / "proc/modules", "")
    write(host / "etc/resolv.conf", "nameserver 8.8.8.8\n")
    write(host / "etc/fstab", "# fake fstab sentinel; preflight must not change this\n")
    write(host / "dev/kvm", "", 0o666)
    write(host / "sys/fs/cgroup/cgroup.controllers", "cpu memory io\n")
    for directory in (
        host / "run/systemd/system",
        host / "run/lock",
        host / "sys/fs/bpf",
        host / "data/cubelet",
        install_root.parent,
        host / "var/run/cube-sandbox-one-click",
        host / "var/log/cube-sandbox-one-click",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    write(fake_bin / "uname", """#!/usr/bin/env bash
case "${1:-}" in
  -m) printf 'x86_64\\n' ;;
  -r) printf '6.6.0-fake\\n' ;;
  *) printf 'Linux\\n' ;;
esac
""", 0o755)
    write(fake_bin / "nproc", "#!/usr/bin/env bash\nprintf '8\\n'\n", 0o755)
    write(fake_bin / "ldd", "#!/usr/bin/env bash\nprintf 'ldd (GNU libc) 2.31\\n'\n", 0o755)
    write(fake_bin / "docker", "#!/usr/bin/env bash\nexit 0\n", 0o755)
    write(fake_bin / "ip", """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  '-4 route show default')
    printf 'default via 192.0.2.1 dev eth0\\n'
    ;;
  '-4 -o addr show dev eth0 scope global')
    printf '2: eth0 inet 192.0.2.10/24 scope global eth0\\n'
    ;;
  '-4 -o addr show scope global')
    count="${FAKE_GLOBAL_IP_COUNT:-1}"
    for ((index = 0; index < count; index++)); do
      printf '2: eth0 inet 192.0.2.%d/24 scope global eth0\\n' "$((index + 10))"
    done
    ;;
  '-4 -o addr show')
    printf '2: eth0 inet 192.0.2.10/24 scope global eth0\\n'
    ;;
  '-4 route show table all')
    printf 'default via 192.0.2.1 dev eth0\\n192.0.2.0/24 dev eth0 proto kernel scope link\\n'
    ;;
  *)
    printf 'unexpected fake ip invocation: %s\\n' "$*" >&2
    exit 90
    ;;
esac
""", 0o755)
    write(fake_bin / "ss", """#!/usr/bin/env bash
if [[ "${FAKE_PORT_BUSY:-0}" == "1" ]]; then
  printf 'LISTEN 0 128 0.0.0.0:3000 0.0.0.0:*\\n'
fi
""", 0o755)
    write(fake_bin / "stat", """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "-fc" && "${2:-}" == "%T" ]] || exit 90
case "$3" in
  */sys/fs/bpf) printf 'bpf\\n' ;;
  */sys/fs/cgroup) printf 'cgroup2fs\\n' ;;
  *) printf 'xfs\\n' ;;
esac
""", 0o755)
    write(fake_bin / "df", """#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
printf 'fake 100000000 1 99999999 1%% /\\n'
""", 0o755)
    for command in ("mount", "modprobe"):
        write(fake_bin / command, """#!/usr/bin/env bash
printf '%s\\n' "$0 $*" >> "${FAKE_FORBIDDEN_LOG:?}"
exit 90
""", 0o755)
    write(systemctl, """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_SYSTEMCTL_LOG:?}"
case " $* " in
  *' start '*|*' stop '*|*' restart '*|*' enable '*|*' disable '*)
    printf 'state-changing systemctl call is forbidden in tier-A: %s\\n' "$*" >&2
    exit 90
    ;;
esac
if [[ "${FAKE_SYSTEMD_HEALTHY:-1}" != "1" ]]; then
  printf 'failed\\n'
  exit 1
fi
printf 'active\\n'
""", 0o755)
    write(fake_bin / "curl", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_CURL_LOG:?}"
config=''
output=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      config="$2"
      shift 2
      ;;
    --output)
      output="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [[ -n "${config}" ]]; then
  [[ -f "${config}" && -n "${output}" && -f "${FAKE_BUNDLE:?}" ]]
  cp "${FAKE_BUNDLE}" "${output}"
  exit 0
fi
[[ "${FAKE_HEALTHY:-1}" == "1" ]] || {
  printf 'fake health endpoint failed\\n' >&2
  exit 22
}
printf 'OK\\n'
""", 0o755)

    bundle_root = target / "bundle-src/cube-sandbox-one-click-v1.2.3-amd64"
    quickcheck = """#!/usr/bin/env bash
set -euo pipefail
printf 'started\\n' >> "${FAKE_QUICKCHECK_LOG:?}"
if [[ "${FAKE_REQUIRE_RUNTIME_ENV:-0}" == "1" ]]; then
  [[ "${ONE_CLICK_RUNTIME_ENV_FILE:-}" == "__INSTALL_ROOT__/.one-click.env" ]] || {
    printf 'runtime env override escaped the verifier\n' >&2
    exit 86
  }
fi
if [[ "${FAKE_VERIFY_CANARY:-0}" == "1" ]]; then
  echo 'https://signed-user:fake-token@example.invalid/private/egress/dump?X-Amz-Signature=signed-query-secret'
fi
echo '[quickcheck] role=control'
echo '[quickcheck] check systemd units'
state="$(systemctl show -p ActiveState --value cube-sandbox-control.target)"
[[ "${state}" == "active" ]]
echo '[quickcheck] 1/4 check Cubelet embedded network runtime egress dump'
curl -fsS http://127.0.0.1:9998/v1/policies/dump >/dev/null
echo '[quickcheck] 2/4 check cubemaster /notify/health'
curl -fsS http://127.0.0.1:8089/notify/health >/dev/null
echo '[quickcheck] 3/5 check cube-api /health'
curl -fsS http://127.0.0.1:3000/health >/dev/null
echo '[quickcheck] 4/5 check cubeops /health'
curl -fsS http://127.0.0.1:3010/health >/dev/null
echo '[quickcheck] OK'
"""
    quickcheck = quickcheck.replace("__INSTALL_ROOT__", str(install_root))
    write(bundle_root / "scripts/one-click/quickcheck.sh", quickcheck, 0o755)
    write(bundle_root / "scripts/one-click/common.sh", "# fake official quickcheck common library\n")
    installer = """#!/usr/bin/env bash
set -euo pipefail
{
  printf 'mode=%s\\n' "${ONE_CLICK_MODE:-}"
  printf 'quickcheck=%s\\n' "${ONE_CLICK_RUN_QUICKCHECK:-}"
  printf 'role=%s\\n' "${ONE_CLICK_DEPLOY_ROLE:-}"
} > "${FAKE_INSTALL_RECORD:?}"
[[ "${ONE_CLICK_MODE:-}" == "install" ]]
[[ "${ONE_CLICK_RUN_QUICKCHECK:-}" == "1" ]]
[[ "${ONE_CLICK_DEPLOY_ROLE:-}" == "control" ]]
[[ -f "${ONE_CLICK_ENV_FILE:?}" ]]
if grep -Eq 'CUBE_SANDBOX_DOWNLOAD_URL|CUBE_SANDBOX_SHA256|fake-token' "${ONE_CLICK_ENV_FILE}"; then
  printf 'wrapper-only URL or secret leaked to upstream env\\n' >&2
  exit 80
fi
mkdir -p "__INSTALL_ROOT__/scripts/one-click"
cp "__BUNDLE_ROOT__/scripts/one-click/quickcheck.sh" "__INSTALL_ROOT__/scripts/one-click/quickcheck.sh"
cp "__BUNDLE_ROOT__/scripts/one-click/common.sh" "__INSTALL_ROOT__/scripts/one-click/common.sh"
printf 'ONE_CLICK_DEPLOY_ROLE=control\\nCUBE_SANDBOX_NODE_IP=192.0.2.10\\nVERIFY_SECRET=verify-secret-do-not-log\\n' > "__INSTALL_ROOT__/.one-click.env"
chmod 600 "__INSTALL_ROOT__/.one-click.env"
bash "__INSTALL_ROOT__/scripts/one-click/quickcheck.sh"
"""
    installer = installer.replace("__INSTALL_ROOT__", str(install_root)).replace("__BUNDLE_ROOT__", str(bundle_root))
    write(bundle_root / "install.sh", installer, 0o755)
    bundle = target / "cube-sandbox-one-click-v1.2.3-amd64.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(bundle_root, arcname=bundle_root.name)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    common_env = """CUBE_VERSION=v1.2.3
MIRROR=
CUBE_PVM_ENABLE=0
CUBE_SANDBOX_NETWORK_CIDR=10.100.0.0/18
CUBE_SANDBOX_DOWNLOAD_URL=https://user:fake-token@example.invalid/cube-sandbox-one-click-v1.2.3-amd64.tar.gz
CUBE_SANDBOX_SHA256=__DIGEST__
"""
    env_text = common_env.replace("__DIGEST__", digest)
    write(target / "deployment.env", env_text + "CUBE_SANDBOX_NODE_IP=192.0.2.10\n", 0o600)
    write(target / "deployment-no-node.env", env_text, 0o600)
    write(target / "deployment-bad-sha.env", (env_text + "CUBE_SANDBOX_NODE_IP=192.0.2.10\n").replace(digest, "0" * 64), 0o600)
    no_sha_env = "\n".join(
        line for line in env_text.splitlines() if not line.startswith("CUBE_SANDBOX_SHA256=")
    ) + "\nCUBE_SANDBOX_NODE_IP=192.0.2.10\n"
    write(target / "deployment-no-sha.env", no_sha_env, 0o600)
    write(target / "deployment-drift.env", (env_text + "CUBE_SANDBOX_NODE_IP=192.0.2.10\n").replace("v1.2.3", "v1.2.4"), 0o600)
    summary = {
        "layout": str(layout),
        "host": str(host),
        "fake_bin": str(fake_bin),
        "run_tmp": str(run_tmp),
        "install_root": str(install_root),
        "bundle": str(bundle),
        "bundle_root": str(bundle_root),
        "env": str(target / "deployment.env"),
        "no_node_env": str(target / "deployment-no-node.env"),
        "bad_sha_env": str(target / "deployment-bad-sha.env"),
        "no_sha_env": str(target / "deployment-no-sha.env"),
        "drift_env": str(target / "deployment-drift.env"),
        "sha256": digest,
    }
    write(target / "fixture.json", json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
