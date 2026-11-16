#!/usr/bin/env python3
"""Collect a read-only DiskANN benchmark environment inventory as JSON."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run(command: list[str], cwd: Path | None = None) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"available": False, "command": command, "returncode": None, "stdout": "", "stderr": "not found"}
    try:
        completed = subprocess.run(
            command, cwd=str(cwd) if cwd else None,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "command": command, "returncode": None,
                "stdout": "", "stderr": str(exc)}
    return {
        "available": True,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def file_info(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    info: dict[str, Any] = {"path": str(resolved), "exists": resolved.exists()}
    if resolved.exists():
        stat = resolved.stat()
        info.update({"size_bytes": stat.st_size, "mode": oct(stat.st_mode & 0o777), "device": stat.st_dev})
        info["filesystem"] = run(["findmnt", "-T", str(resolved), "-J"])
        if resolved.is_file():
            info["filefrag"] = run(["filefrag", "-v", str(resolved)])
    return info


def repository_info(repo: Path) -> dict[str, Any]:
    root = run(["git", "rev-parse", "--show-toplevel"], repo)
    owns_source = (root["returncode"] == 0 and root["stdout"]
                   and Path(root["stdout"]).resolve() == repo)
    info = {"path": str(repo), "git_root": root}
    for field, arguments in (("commit", ["rev-parse", "HEAD"]),
                             ("branch", ["branch", "--show-current"]),
                             ("status", ["status", "--short"])):
        info[field] = run(["git", *arguments], repo) if owns_source else {
            "available": False, "returncode": None, "stdout": "",
            "stderr": "source is not a Git root; record version and source digest",
        }
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="DiskANN repository")
    parser.add_argument("--index", type=Path, help="Optional disk index file")
    parser.add_argument("--output", type=Path, help="Write JSON to this path")
    args = parser.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: repository is not a directory: {repo}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "schema_version": "diskann-env.v1",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "hostname": platform.node(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
        },
        "repository": repository_info(repo),
        "commands": {
            "uname": run(["uname", "-a"]),
            "lscpu": run(["lscpu", "-J"]),
            "numa": run(["numactl", "--hardware"]),
            "rustc": run(["rustc", "--version", "--verbose"]),
            "cargo": run(["cargo", "--version"]),
            "gcc": run(["gcc", "--version"]),
            "clang": run(["clang", "--version"]),
            "perf": run(["perf", "--version"]),
            "iostat": run(["iostat", "-V"]),
        },
        "index": file_info(args.index) if args.index else None,
    }

    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
