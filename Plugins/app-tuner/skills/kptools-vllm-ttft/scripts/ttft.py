#!/usr/bin/env python3
"""v0.27 TTFT skill — one entrypoint.

Subcommands:
  inspect     identify the vllm / vllm_ascend source roots a Python imports
  diagnose    collect read-only import evidence and an existing startup traceback
  plan        dry-run: which files would receive instrumentation
  apply       insert instrumentation (line-hunk engine, preserves local edits)
  rollback    restore pre-apply state from the state file
  check       log preflight: are all diagnostic markers present?
  report      generate the TTFT HTML report (user-selected gen_ttft_report 9)

Typical flow: see SKILL.md for the existing-uv/serving-interpreter wrapper.
Run inspect/plan read-only; user confirms and stops P workers before apply.
Use a fresh persistent state directory for each component and attempt:
  ttft inspect --python "$PY"
  ttft plan --python "$PY" --repo vllm
  ttft apply --python "$PY" --repo vllm --state-dir "$STATE/vllm"
  ttft check log_prefill.log
  ttft report log_prefill.log -o ttft_report.html
"""

from __future__ import annotations

import argparse
from importlib.machinery import EXTENSION_SUFFIXES
import sys
from pathlib import Path

def check_release_import_safety() -> None:
    """Refuse a contaminated extraction before adding its modules to sys.path."""
    scripts = Path(__file__).resolve().parent
    collisions = [
        base / ("mooncake" + suffix)
        for base in (scripts, scripts.parent)
        for suffix in ("", ".py", ".pyc", *EXTENSION_SUFFIXES)
        if (base / ("mooncake" + suffix)).exists()
    ]
    if collisions:
        raise SystemExit(
            "ERROR: skill release contains Mooncake import collision(s): "
            + ", ".join(str(path) for path in collisions)
            + ". Preserve this directory and extract a clean release into a NEW directory; "
            "do not overwrite it or delete serving dependencies. Nothing was changed."
        )


check_release_import_safety()
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagnose as diagnose_mod
import report as report_mod
import ttft_skill as core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ttft", description="v0.27 TTFT instrumentation + report skill"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    core.register_cli(sub)
    report_mod.register(sub)
    diagnose_mod.register(sub)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except core.SkillError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
