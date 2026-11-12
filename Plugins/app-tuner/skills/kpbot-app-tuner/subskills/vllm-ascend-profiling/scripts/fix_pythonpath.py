#!/usr/bin/env python3
"""Stage 1 fix — install the rosinL/vllm TTFT-breakdown fork and set PYTHONPATH.

Consent-based: the agent asks the user BEFORE running this script; the script
itself never prompts and never restarts vLLM. It only:

  1. validates/installs the fork source inside the container — an existing
     checkout is reused when the commit markers match; otherwise the fork
     tarball (references/vllm_fork.json tarball_url) is downloaded DIRECTLY
     inside the container via curl and extracted (no offline/local fallback)
  2. writes the profile.d file exporting PYTHONPATH (idempotent)

PYTHONPATH only takes effect at process start, so after this script the USER
restarts the vLLM service and Stage 1 (env_check.py) is re-run to verify.

Usage:
  python3 fix_pythonpath.py --container <name_or_id> [--fork-path /dir]
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

FORK_REF_PATH = Path(__file__).resolve().parent.parent / "references" / "vllm_fork.json"

# fork_path lands inside /etc/profile.d/*.sh which every login shell sources;
# enforce a strict absolute-path whitelist (newline/CR/spaces/metachars fail
# fullmatch and are rejected before any command is built).
_FORK_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def docker_exec(container: str, inner: str, timeout: int = 20) -> tuple[int, str]:
    return _run(["docker", "exec", container, "bash", "-c", inner], timeout=timeout)


def load_fork_ref() -> dict[str, Any]:
    try:
        with open(FORK_REF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read {FORK_REF_PATH}: {e}"}), file=sys.stderr)
        sys.exit(2)


def verify_fork(container: str, fork_path: str, markers: dict[str, str]) -> tuple[bool, str]:
    code, out = docker_exec(container, f"test -f {shlex.quote(fork_path + '/vllm/__init__.py')} && echo FOUND || true")
    if "FOUND" not in out:
        return False, f"{fork_path}/vllm/__init__.py not found"
    parts = []
    for rel, marker in markers.items():
        parts.append(
            f"grep -qF -- {shlex.quote(marker)} {shlex.quote(fork_path + '/' + rel)} 2>/dev/null"
            f" && echo 'ok {rel}' || echo 'missing {rel}'"
        )
    code, out = docker_exec(container, "; ".join(parts), timeout=60)
    missing = [l for l in out.splitlines() if l.startswith("missing")]
    if missing:
        return False, f"markers missing in {fork_path}: {missing}"
    return True, f"checkout at {fork_path} carries all fork markers"


def install_fork(container: str, tarball_url: str, fork_path: str, timeout: int) -> tuple[bool, str]:
    """Download the fork tarball DIRECTLY inside the container and extract it.

    No offline/vendored fallback: the single source of truth is tarball_url.
    """
    inner = (
        f"mkdir -p {shlex.quote(fork_path)} && "
        f"curl -fsSL {shlex.quote(tarball_url)} | tar xz -C {shlex.quote(fork_path)} --strip-components=1"
    )
    code, out = docker_exec(container, inner, timeout=timeout)
    if code != 0:
        return False, f"direct download/extract failed (exit {code}): {out[-500:]}"
    return True, f"downloaded {tarball_url} into {fork_path}"


def write_profile(container: str, profile_file: str, fork_path: str) -> tuple[bool, str, str]:
    """Write a profile.d file exporting PYTHONPATH (idempotent, hardened).

    fork_path is validated upstream to ^/[A-Za-z0-9._/-]+$ and additionally
    written shell-escaped inside single quotes. The verdict reads back the
    FULL file: exactly one non-comment export line is accepted, so a legacy
    or tampered file with extra statements is rewritten, never trusted. A
    login shell sources this file, which is why the extra-state check is not
    a substring test.
    """
    value_esc = fork_path.replace("'", "'\\''")
    export_line = f"export PYTHONPATH='{value_esc}'${{PYTHONPATH:+:$PYTHONPATH}}"

    code, out = docker_exec(container, f"cat {shlex.quote(profile_file)} 2>/dev/null || true")
    if code == 0 and _profile_export_lines(out) == [export_line]:
        return True, f"{profile_file} already exports this PYTHONPATH", "already-set"

    content = f"# set by vllm-ascend-profiling fix_pythonpath.py\n{export_line}\n"
    inner = f"printf '%s\\n' {shlex.quote(content)} > {shlex.quote(profile_file)}"
    code, out = docker_exec(container, inner)
    if code != 0:
        return False, f"cannot write {profile_file}: {out[:300]}", "write-failed"

    code, out = docker_exec(container, f"cat {shlex.quote(profile_file)}")
    if code != 0:
        return False, f"cannot read back {profile_file}: {out[:300]}", "verify-failed"
    if _profile_export_lines(out) != [export_line]:
        return False, f"{profile_file} content mismatch after write: {out[:300]!r}", "verify-failed"
    return True, f"wrote {profile_file}: {export_line}", "written"


def _profile_export_lines(out: str) -> list[str]:
    """Non-comment, non-empty lines of a profile.d file (the executable state)."""
    lines = [l.rstrip("\r") for l in out.splitlines()]
    return [l for l in lines if l.strip() and not l.strip().startswith("#")]


def _invalid_fork_path_reason(fork_path: str) -> str | None:
    """Whitelist the fork path BEFORE it reaches a sourced profile.d file.

    The value ends up inside /etc/profile.d/*.sh, which every login shell
    sources s root. Anything outside this whitelist (spaces, shell
    metachars, backticks, newline/CR, Windows drive letters, relative paths)
    is rejected outright instead of escaped-and-hoped.
    """
    if not _FORK_PATH_RE.fullmatch(fork_path):
        return (
            "must be an absolute path made only of [A-Za-z0-9._/-] "
            "(no spaces, shell metachars, or newlines)"
        )
    return None


def _resolve_container(explicit: str) -> str:
    """Use the explicit container or auto-detect the vLLM one (see container_utils)."""
    if explicit:
        return explicit
    import container_utils

    det = container_utils.detect_vllm_container()
    if "error" in det:
        print(
            json.dumps(
                {"error": det["error"], "candidates": det.get("candidates", []),
                 "hint": "pass --container explicitly"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        json.dumps({"container_detected": det["container"], "detail": det["matched_by"]}, ensure_ascii=False),
        file=sys.stderr,
    )
    return det["container"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--container", default="", help="docker container name/id running the vLLM service")
    ap.add_argument(
        "--fork-path",
        default="",
        help="override the in-container fork source dir (default: references/vllm_fork.json container_path)",
    )
    ap.add_argument("--download-timeout", type=int, default=600, help="seconds allowed for the tarball download")
    args = ap.parse_args()
    args.container = _resolve_container(args.container)

    ref = load_fork_ref()
    fork_path = args.fork_path or ref.get("container_path", "")
    bad_path = _invalid_fork_path_reason(fork_path)
    if bad_path:
        print(
            json.dumps({"error": f"invalid fork path {fork_path!r}: {bad_path}"} , ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    profile_file = ref.get("profile_file", "/etc/profile.d/vllm_ttft_fork.sh")
    markers = ref.get("markers", {})
    if not fork_path or not markers or not ref.get("tarball_url"):
        print(
            json.dumps({"error": f"reference {FORK_REF_PATH} lacks container_path/tarball_url/markers"}),
            file=sys.stderr,
        )
        return 2

    code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", args.container])
    if code != 0:
        print(json.dumps({"error": f"container {args.container!r} not reachable"}), file=sys.stderr)
        return 2

    actions: list[dict[str, Any]] = []

    ok, detail = verify_fork(args.container, fork_path, markers)
    if ok:
        actions.append({"action": "reuse-checkout", "status": "pass", "detail": detail})
    else:
        actions.append({"action": "reuse-checkout", "status": "fail", "detail": detail})
        ok, detail = install_fork(
            args.container, ref["tarball_url"], fork_path, args.download_timeout
        )
        actions.append(
            {"action": "install-fork-download", "status": "pass" if ok else "fail", "detail": detail}
        )
        if not ok:
            return emit(actions, failed=True)
        ok, detail = verify_fork(args.container, fork_path, markers)
        actions.append({"action": "verify-fork", "status": "pass" if ok else "fail", "detail": detail})
        if not ok:
            return emit(actions, failed=True)

    ok, detail, action = write_profile(args.container, profile_file, fork_path)
    actions.append({"action": action, "status": "pass" if ok else "fail", "detail": detail})
    return emit(actions, failed=not ok)


def emit(actions: list[dict[str, Any]], failed: bool) -> int:
    payload = {
        "stage": "1-fix-pythonpath",
        "results": actions,
        "restart_required": True,
        "next": "User restarts the vLLM service, then re-run env_check.py --container <name>",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
