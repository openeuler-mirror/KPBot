#!/usr/bin/env python3
"""Stage 2b — run torch_npu analyse() in-container on every *_ascend_pt export dir.

Runs AFTER the profiling window (POST /stop_profile) and BEFORE collect_data
archives the raw data, so the generated ``trace_view.json`` files are archived
together with the raw exports by the regular collect step. No sync-back logic
is needed.

For each ``dp*_pp*_tp*_*_ascend_pt`` directory under the profiler dir
(``--profiler-dir``; ASK THE USER — the VLLM_TORCH_PROFILER_DIR value read from
the vllm process env is only reported as a candidate hint, never used
automatically), runs inside the container::

    python3 -c "from torch_npu.profiler.profiler import analyse; analyse(profiler_path='<dir>')"

Dirs that already contain a trace_view.json are skipped (idempotent). This
helper never touches the vLLM service; it only adds files inside the collected
profiler output dirs.

Usage:
  python3 analyse_trace.py --container <name> --profiler-dir <用户确认的容器内目录>
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from typing import Any


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def docker_exec(container: str, inner: str, timeout: int = 60, login: bool = False) -> tuple[int, str]:
    flag = "-lc" if login else "-c"
    return _run(["docker", "exec", container, "bash", flag, inner], timeout=timeout)


def resolve_python(container: str) -> str:
    code, out = docker_exec(container, "ps -ef | grep vllm | grep -v grep")
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[1].isdigit():
            tok = parts[7].split()
            if tok and "python" in tok[0].rsplit("/", 1)[-1] and tok[0].startswith("/"):
                return tok[0]
    return "python3"


def detect_profiler_dir_hint(container: str) -> str:
    """Read VLLM_TORCH_PROFILER_DIR from the vllm process env as a candidate.

    Never used directly — the dir must be confirmed by the user and passed via
    --profiler-dir. No filesystem traversal happens here (only /proc environ).
    """
    code, out = docker_exec(container, "ps -ef | grep vllm | grep -v grep")
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[1].isdigit():
            continue
        pid = parts[1]
        code, env = docker_exec(
            container,
            f"tr '\\0' '\\n' < /proc/{pid}/environ | grep -E '^VLLM_TORCH_PROFILER_DIR=' || true",
        )
        for l in env.splitlines():
            if l.startswith("VLLM_TORCH_PROFILER_DIR="):
                return l.split("=", 1)[1]
    return ""


def list_ascend_pt_dirs(container: str, profiler_dir: str) -> list[str]:
    code, out = docker_exec(
        container,
        f"ls -1d {shlex.quote(profiler_dir)}/*_ascend_pt 2>/dev/null || true",
    )
    return [l.strip() for l in out.splitlines() if l.strip().endswith("_ascend_pt")]


def find_trace_view(container: str, ascend_pt_dir: str) -> str:
    code, out = docker_exec(
        container,
        f"find {shlex.quote(ascend_pt_dir)} -name trace_view.json -type f 2>/dev/null || true",
    )
    for l in out.splitlines():
        l = l.strip()
        if l.endswith("trace_view.json"):
            return l
    return ""


def run_analyse(container: str, py: str, ascend_pt_dir: str, timeout: int) -> tuple[bool, str]:
    inner = (
        f"{shlex.quote(py)} -c "
        + shlex.quote(
            f"from torch_npu.profiler.profiler import analyse; analyse(profiler_path={ascend_pt_dir!r})"
        )
    )
    code, out = docker_exec(container, inner, timeout=timeout)
    if code == 0:
        return True, out[-300:]
    # retry with a login shell: some images only export CANN env via profile
    code2, out2 = docker_exec(container, inner, timeout=timeout, login=True)
    detail = f"bash -c exit {code}: {out[-200:]}; bash -lc exit {code2}: {out2[-200:]}"
    return code2 == 0, detail


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
        "--profiler-dir",
        default="",
        help="container-side dir holding *_ascend_pt exports — ASK THE USER; the env-var value "
        "is only reported as a candidate hint, never used automatically",
    )
    ap.add_argument("--timeout", type=int, default=900, help="per-dir analyse timeout seconds")
    args = ap.parse_args()
    args.container = _resolve_container(args.container)

    code, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", args.container])
    if code != 0:
        print(json.dumps({"error": f"container {args.container!r} not reachable"}), file=sys.stderr)
        return 2

    profiler_dir = args.profiler_dir
    if not profiler_dir:
        hint = detect_profiler_dir_hint(args.container)
        hint_msg = (
            f" the vllm process reports VLLM_TORCH_PROFILER_DIR={hint!r} — confirm with the user"
            if hint
            else " ask the user for the container-side VLLM_TORCH_PROFILER_DIR"
        )
        print(
            json.dumps({"error": "profiler dir not provided;" + hint_msg + " and pass --profiler-dir"}),
            file=sys.stderr,
        )
        return 2

    dirs = list_ascend_pt_dirs(args.container, profiler_dir)
    if not dirs:
        print(
            json.dumps({"error": f"no *_ascend_pt dirs under {profiler_dir} (collect a profile first?)"}),
            file=sys.stderr,
        )
        return 1

    py = resolve_python(args.container)
    actions: list[dict[str, Any]] = []
    failed = False
    for d in dirs:
        name = d.rsplit("/", 1)[-1]
        tv = find_trace_view(args.container, d)
        if tv:
            actions.append({"action": "skip", "dir": name, "status": "pass", "detail": f"already has {tv}"})
            continue
        ok, detail = run_analyse(args.container, py, d, args.timeout)
        if not ok:
            actions.append({"action": "analyse", "dir": name, "status": "fail", "detail": detail})
            failed = True
            continue
        tv = find_trace_view(args.container, d)
        if not tv:
            actions.append(
                {"action": "analyse", "dir": name, "status": "fail", "detail": "analyse exited 0 but no trace_view.json found"}
            )
            failed = True
            continue
        actions.append({"action": "analyse", "dir": name, "status": "pass", "detail": tv})

    payload = {
        "stage": "2b-analyse",
        "container": args.container,
        "profiler_dir": profiler_dir,
        "python": py,
        "results": actions,
        "next": "collect_data.py --src <host-side mount of the profiler dir> (archives raw + trace_view.json together)",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
