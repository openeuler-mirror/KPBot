#!/usr/bin/env python3
"""Shared helper: auto-detect the docker container running the vLLM service.

Detection strategy (process-based, no name/image guessing):
  1. list running containers (docker ps)
  2. probe each with a read-only `ps -ef | grep vllm` inside it
     (numeric-pid guarded against shell noise, bounded timeout)
  3. exactly one container has a real vllm process -> use it;
     multiple -> report them for the user to pick; none -> report failure

Scripts import this sibling module (same directory) and call
``detect_vllm_container()`` when ``--container`` is omitted.
"""

from __future__ import annotations

import subprocess
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


def list_running_containers() -> list[dict[str, str]]:
    code, out = _run(["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"])
    if code != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"id": parts[0].strip(), "names": parts[1].strip(), "image": parts[2].strip()})
    return rows


def has_vllm_process(container: str) -> bool:
    code, out = _run(
        ["docker", "exec", container, "bash", "-c", "ps -ef | grep vllm | grep -v grep || true"],
        timeout=15,
    )
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[1].isdigit():
            return True
    return False


def detect_vllm_container() -> dict[str, Any]:
    """Return {"container", "matched_by"} on success or {"error", "candidates"}."""
    containers = list_running_containers()
    if not containers:
        return {"error": "docker unreachable or no running containers", "candidates": []}

    # probe every running container by vllm PROCESS presence (read-only ps,
    # bounded timeout) — never by container/image name
    matches = [c for c in containers if has_vllm_process(c["id"])]

    if len(matches) == 1:
        c = matches[0]
        return {
            "container": c["names"] or c["id"],
            "matched_by": (
                f"vllm process found in this container (id={c['id'][:12]}, image={c['image']}, "
                f"name={c['names'] or '-'})"
            ),
        }
    if len(matches) > 1:
        return {
            "error": "multiple running containers have a vllm process — ask the user to pick",
            "candidates": [c["names"] or c["id"] for c in matches],
        }
    return {
        "error": "no running container has a vllm process",
        "candidates": [c["names"] or c["id"] for c in containers],
    }
