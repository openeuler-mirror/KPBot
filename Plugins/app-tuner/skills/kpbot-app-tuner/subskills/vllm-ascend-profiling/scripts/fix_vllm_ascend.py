#!/usr/bin/env python3
"""Stage 1 fix — replace the two TTFT-timing patched vllm_ascend source files.

Consent-based: the agent asks the user BEFORE running this script; the script
itself never prompts and never restarts vLLM. It only:

  1. resolves the installed vllm-ascend via `pip show` and verifies the base
     version matches references/vllm_ascend.json (expected_version_substr) —
     the stored files are built against that exact base, so on mismatch the
     script aborts without touching anything
  2. for each target file: skips when all patch markers are already present
     (idempotent); otherwise backs up `<file>.bak-<timestamp>` inside the
     container and docker-cp's the stored patched file over it
  3. re-verifies the markers inside the container

vllm_ascend is imported at process start, so after this script the USER
restarts the vLLM service and Stage 1 (env_check.py) is re-run.

Usage:
  python3 fix_vllm_ascend.py --container <name_or_id>
"""

from __future__ import annotations

import argparse
import datetime
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
REF_PATH = SKILL_DIR / "references" / "vllm_ascend.json"


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


def docker_cp_to_container(container: str, local: Path, remote: str) -> tuple[int, str]:
    return _run(["docker", "cp", str(local), f"{container}:{remote}"], timeout=60)


def load_ref() -> dict[str, Any]:
    try:
        with open(REF_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read {REF_PATH}: {e}"}), file=sys.stderr)
        sys.exit(2)


def resolve_python(container: str) -> str:
    code, out = docker_exec(container, "ps -ef | grep vllm | grep -v grep")
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8:
            tok = parts[7].split()
            if tok and "python" in tok[0].rsplit("/", 1)[-1] and tok[0].startswith("/"):
                return tok[0]
    return "python3"


def pip_show(container: str, py: str, pkg: str) -> tuple[str, str]:
    code, out = docker_exec(
        container,
        f"{shlex.quote(py)} -m pip show {pkg} 2>/dev/null || pip3 show {pkg} 2>/dev/null || true",
    )
    version = location = ""
    for line in out.splitlines():
        if line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("Location:"):
            location = line.split(":", 1)[1].strip()
    return version, location


def markers_present(container: str, target: str, markers: list[str]) -> tuple[bool, str]:
    parts = [
        f"grep -qF -- {shlex.quote(m)} {shlex.quote(target)} 2>/dev/null"
        f" && echo 'ok {i}' || echo 'missing {i}'"
        for i, m in enumerate(markers)
    ]
    code, out = docker_exec(container, "; ".join(parts), timeout=30)
    missing = [markers[int(l.split()[1])] for l in out.splitlines() if l.startswith("missing")]
    return not missing, ", ".join(missing) if missing else "all present"


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
    args = ap.parse_args()
    args.container = _resolve_container(args.container)

    ref = load_ref()
    pkg = ref.get("pip_package", "vllm-ascend")
    substr = ref.get("expected_version_substr", "")
    patch_dir = SKILL_DIR / "references" / ref.get("patch_dir", "vllm_ascend_patch")

    code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", args.container])
    if code != 0:
        print(json.dumps({"error": f"container {args.container!r} not reachable"}), file=sys.stderr)
        return 2

    py = resolve_python(args.container)
    version, location = pip_show(args.container, py, pkg)
    if not version or substr not in version:
        print(
            json.dumps(
                {
                    "error": (
                        f"vllm-ascend base version mismatch: pip reports {version or '<not installed>'!r} "
                        f"but expected a version containing {substr!r} "
                        f"({ref.get('repo')}@{str(ref.get('commit', ''))[:7]}). "
                        "Refusing to replace files on a different base."
                    )
                }
            ),
            file=sys.stderr,
        )
        return 1
    if not location:
        print(json.dumps({"error": "pip show did not report a Location for vllm-ascend"}), file=sys.stderr)
        return 1

    actions: list[dict[str, Any]] = []
    actions.append({"action": "resolve", "status": "pass", "detail": f"py={py} version={version} location={location}"})

    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    failed = False
    for relpath, spec in ref["patch_files"].items():
        target = f"{location}/{relpath}"
        local = patch_dir / spec["local"]
        if not local.is_file():
            actions.append({"action": "replace", "status": "fail", "detail": f"stored file missing: {local}"})
            failed = True
            continue

        code, out = docker_exec(args.container, f"test -f {shlex.quote(target)} && echo FOUND || true")
        if "FOUND" not in out:
            actions.append({"action": "replace", "status": "fail", "detail": f"target not found in container: {target}"})
            failed = True
            continue

        ok, detail = markers_present(args.container, target, spec.get("markers", []))
        if ok:
            actions.append({"action": "skip", "status": "pass", "detail": f"{relpath}: already patched"})
            continue

        code, out = docker_exec(
            args.container, f"cp {shlex.quote(target)} {shlex.quote(target + '.bak-' + ts)}"
        )
        if code != 0:
            actions.append({"action": "backup", "status": "fail", "detail": out[:300]})
            failed = True
            continue
        actions.append({"action": "backup", "status": "pass", "detail": f"{target}.bak-{ts}"})

        code, out = docker_cp_to_container(args.container, local, target)
        if code != 0:
            actions.append({"action": "replace", "status": "fail", "detail": out[:300]})
            failed = True
            continue

        ok, detail = markers_present(args.container, target, spec.get("markers", []))
        actions.append(
            {"action": "replace", "status": "pass" if ok else "fail", "detail": f"{relpath}: replaced; markers {detail}"}
        )
        if not ok:
            failed = True

    payload = {
        "stage": "1-fix-vllm-ascend",
        "results": actions,
        "restart_required": True,
        "next": "User restarts the vLLM service, then re-run env_check.py --container <name>",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
