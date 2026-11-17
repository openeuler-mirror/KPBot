"""Run regressions using the A/B runner's capability and resource settings."""
import argparse
import subprocess
import uuid
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SECURITY = SKILL / "cases/agent-offline-boundary/docker_security.sh"


def security_args():
    output = subprocess.check_output([
        "bash", "-c", 'source "$1"; printf "%s\\0" "${CASE_DOCKER_SECURITY_ARGS[@]}"',
        "bash", str(SECURITY),
    ])
    return [value.decode() for value in output.split(b"\0") if value]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Existing local Linux image with Python 3 and sha256sum")
    parser.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args()
    name = "kpbot-prompt-regression-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--rm", "--pull=never", "--platform", args.platform,
               "--network=none", "--name", name, *security_args(), "--workdir", "/tmp",
               "--mount", "type=bind,src=" + str(SKILL) + ",dst=/skill,readonly",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", args.image,
               "-m", "unittest", "discover", "-s", "/skill/tests", "-v"]
    try:
        return subprocess.run(command, timeout=180).returncode
    except subprocess.TimeoutExpired:
        return 124
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
