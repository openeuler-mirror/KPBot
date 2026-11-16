#!/usr/bin/env python3
"""Create a file:line evidence index by running ripgrep over a source tree."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("patterns", nargs="+", help="Regular expressions passed to rg")
    parser.add_argument("--glob", action="append", default=[], help="Optional rg glob; repeatable")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2
    if shutil.which("rg") is None:
        print("error: rg is required", file=sys.stderr)
        return 2

    sections: list[str] = ["# Source evidence index", "", f"Repository: `{repo}`", ""]
    for pattern in args.patterns:
        command = ["rg", "--line-number", "--column", "--no-heading", "--color", "never"]
        for item in args.glob:
            command.extend(["--glob", item])
        command.extend(["--", pattern, str(repo)])
        completed = subprocess.run(command, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        sections.extend([f"## `{pattern}`", "", "```text"])
        sections.append(completed.stdout.rstrip() or "<no matches>")
        sections.extend(["```", ""])
        if completed.returncode not in (0, 1):
            print(completed.stderr, file=sys.stderr)
            return completed.returncode
    rendered = "\n".join(sections).rstrip() + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
