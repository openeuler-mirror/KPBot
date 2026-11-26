"""Preflight for the guide-import case; exit 3 when the environment is missing.

The TLS mock, documented bash block and the Python verifier need Python 3.7+,
Bash 4+ (associative arrays), an OpenSSL CLI that can create a SAN certificate,
and the ability to bind a loopback TCP socket. Missing prerequisites are
reported as structured `env_not_satisfied` instead of a skipped-success run.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile


CASE = Path(__file__).resolve().parent


def result_dir():
    return Path(
        os.environ.get("GUIDE_PREFLIGHT_RESULTS", str(CASE / "results"))
    )


def write_result(payload, name="result.json"):
    output = result_dir()
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def clear_stale_result():
    try:
        (result_dir() / "result.json").unlink()
    except FileNotFoundError:
        pass


def python_missing():
    missing = []
    if sys.version_info < (3, 7):
        missing.append("python>=3.7")
    try:
        import http.server  # noqa: F401
        import importlib.util  # noqa: F401
        import ssl  # noqa: F401
        import unittest  # noqa: F401
    except ImportError:
        missing.append("python-stdlib-ssl-http-unittest")
    return missing


def bash_missing():
    bash = shutil.which("bash")
    if bash is None:
        return ["bash>=4"]
    try:
        completed = subprocess.run(
            [bash, "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0 or int(completed.stdout or "0") < 4:
            return ["bash>=4"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return ["bash>=4"]
    return []


def openssl_missing():
    openssl = shutil.which("openssl")
    label = "openssl>=1.1.1 with -addext SAN"
    if openssl is None:
        return [label]
    try:
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "ca.crt"
            key = Path(directory) / "ca.key"
            completed = subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-sha256",
                    "-days",
                    "1",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                capture_output=True,
                timeout=30,
            )
            if (
                completed.returncode != 0
                or not cert.is_file()
                or not key.is_file()
            ):
                return [label]
    except (OSError, subprocess.SubprocessError):
        return [label]
    return []


def loopback_missing():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
    except OSError:
        return ["loopback-bind-127.0.0.1"]
    return []


def missing_requirements():
    missing = python_missing()
    if missing:
        return missing
    missing.extend(bash_missing())
    missing.extend(openssl_missing())
    missing.extend(loopback_missing())
    return missing


def main():
    missing = missing_requirements()
    if missing:
        payload = {
            "success": False,
            "status": "env_not_satisfied",
            "tests": 0,
            "missing": missing,
        }
        write_result(payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 3
    clear_stale_result()
    write_result(
        {"status": "env_satisfied", "missing": []}, name="preflight.json"
    )
    print(json.dumps({"status": "env_satisfied", "missing": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
