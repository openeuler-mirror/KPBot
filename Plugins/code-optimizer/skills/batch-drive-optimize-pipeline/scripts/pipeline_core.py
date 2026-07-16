"""Core batch-pipeline configuration helpers."""

from __future__ import annotations

from argparse import Namespace
from typing import Any


def merge_cli_run_config(manifest: dict[str, Any], args: Namespace) -> dict[str, Any]:
    run_cfg = dict(manifest.get("run") or {})
    if args.timeout_minutes:
        run_cfg["timeout_minutes"] = args.timeout_minutes
    if args.idle_timeout_minutes:
        run_cfg["idle_timeout_minutes"] = args.idle_timeout_minutes
    if args.context_soft_limit_tokens is not None:
        run_cfg["context_soft_limit_tokens"] = args.context_soft_limit_tokens
    if args.context_hard_limit_tokens is not None:
        run_cfg["context_hard_limit_tokens"] = args.context_hard_limit_tokens
    if args.resume_prompt_max_tokens is not None:
        run_cfg["resume_prompt_max_tokens"] = args.resume_prompt_max_tokens
    if args.transcript_tail_lines:
        run_cfg["transcript_tail_lines"] = args.transcript_tail_lines
    return run_cfg
