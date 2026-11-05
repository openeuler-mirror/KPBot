#!/usr/bin/env python3
"""Stage 1 fix — persist PYTHONMALLOC=malloc via /etc/profile.d inside the container.

Consent-based: the agent asks the user BEFORE running this script; the script
itself never prompts and never restarts vLLM. It only writes (idempotently)
/etc/profile.d/vllm_pythonmalloc.sh exporting PYTHONMALLOC=malloc and verifies
the file content.

The env var only takes effect at process start AND /etc/profile.d/ is only
sourced by login shells, so after this script the USER re-enters the container
via a login shell (e.g. docker exec <name> bash -lc "..."), restarts the vLLM
service, and Stage 1 (env_check.py) is re-run to verify.

Usage:
  python3 fix_pythonmalloc.py --container <name_or_id>
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from typing import Any

PROFILE_FILE = "/etc/profile.d/vllm_pythonmalloc.sh"
EXPORT_LINE = "export PYTHONMALLOC=malloc"


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


def write_profile(container: str) -> tuple[bool, str, str]:
    code, out = docker_exec(
        container,
        f"test -f {shlex.quote(PROFILE_FILE)} && grep -qF -- {shlex.quote(EXPORT_LINE)} {shlex.quote(PROFILE_FILE)} && echo PRESENT || true",
    )
    if "PRESENT" in out:
        return True, f"{PROFILE_FILE} already exports PYTHONMALLOC=malloc", "already-set"

    content = f"# set by vllm-ascend-profiling fix_pythonmalloc.py\n{EXPORT_LINE}\n"
    inner = (
        f"mkdir -p /etc/profile.d && "
        f"printf '%s\\n' {shlex.quote(content)} > {shlex.quote(PROFILE_FILE)}"
    )
    code, out = docker_exec(container, inner)
    if code != 0:
        return False, f"cannot write {PROFILE_FILE}: {out[:300]}", "write-failed"

    code, out = docker_exec(container, f"cat {shlex.quote(PROFILE_FILE)}")
    if EXPORT_LINE not in out:
        return False, f"{PROFILE_FILE} content mismatch after write: {out[:300]!r}", "verify-failed"
    return True, f"wrote {PROFILE_FILE}: {EXPORT_LINE}", "written"


def emit(actions: list[dict[str, Any]], failed: bool) -> int:
    payload = {
        "stage": "1-fix-pythonmalloc",
        "results": actions,
        "restart_required": True,
        "next": "Re-enter the container via a login shell (docker exec <name> bash -lc '...') "
        "and restart the vLLM service so /etc/profile.d applies, then re-run "
        "env_check.py --container <name>",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--container", default="", help="docker container name/id running the vLLM service")
    args = ap.parse_args()
    args.container = _resolve_container(args.container)

    code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", args.container])
    if code != 0:
        print(json.dumps({"error": f"container {args.container!r} not reachable"}), file=sys.stderr)
        return 2

    ok, detail, action = write_profile(args.container)
    return emit([{"action": action, "status": "pass" if ok else "fail", "detail": detail}], failed=not ok)


if __name__ == "__main__":
    sys.exit(main())
