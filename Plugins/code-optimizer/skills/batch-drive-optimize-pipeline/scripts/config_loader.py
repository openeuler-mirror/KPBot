"""Configuration loading for the batch-drive pipeline entrypoint."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from data_loader import load_manifest
from pipeline_core import merge_cli_run_config
from task_scheduler import filter_manifest_targets


DEFAULT_MANIFEST = Path("batch_optimize_manifest.yaml")
DEFAULT_BATCH_OUT_PREFIX = "_batch_optimize_results"
DEFAULT_SELF_TEST_OUT_PREFIX = "batch-drive-optimize-self-test"


@dataclass(frozen=True)
class BatchRunContext:
    manifest: dict[str, Any]
    manifest_path: Path | None
    out: Path
    claude_bin: str
    run_cfg: dict[str, Any]
    targets: list[dict[str, Any]]


def timestamp_for_path(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def resolve_default_out(manifest_path: Path | None, self_test: bool, now: datetime | None = None) -> Path:
    stamp = timestamp_for_path(now)
    if self_test:
        return Path(tempfile.gettempdir()) / f"{DEFAULT_SELF_TEST_OUT_PREFIX}_{stamp}"
    base_dir = manifest_path.expanduser().resolve().parent if manifest_path else Path.cwd()
    return base_dir / f"{DEFAULT_BATCH_OUT_PREFIX}_{stamp}"


def resolve_manifest_path(manifest: str | None, default_manifest: str | None) -> Path:
    candidate = Path(manifest).expanduser() if manifest else Path(default_manifest or DEFAULT_MANIFEST).expanduser()
    if not candidate.exists():
        if manifest:
            raise SystemExit(f"manifest not found: {candidate}")
        raise SystemExit(f"--manifest is required because default manifest was not found: {candidate}")
    return candidate.resolve()


def resolve_claude_bin(cli_value: str | None) -> str:
    candidate = cli_value or os.environ.get("CLAUDE_BIN") or "claude"
    has_path_separator = os.path.sep in candidate or (os.path.altsep is not None and os.path.altsep in candidate)
    if has_path_separator:
        path = Path(candidate).expanduser()
        if not path.exists():
            raise SystemExit(f"claude binary not found: {path}; set CLAUDE_BIN or pass --claude-bin")
        return str(path.resolve())
    resolved = shutil.which(candidate)
    if not resolved:
        raise SystemExit(f"claude binary not found: {candidate}; set CLAUDE_BIN or pass --claude-bin")
    return resolved


def validate_args(args: argparse.Namespace) -> None:
    if args.monitor:
        if not args.out:
            raise SystemExit("--monitor requires --out")
        if args.self_test_real_claude or args.manifest or args.targets or args.list_targets:
            raise SystemExit("--monitor cannot be combined with --self-test, --manifest, --targets, or --list-targets")
        return
    if args.self_test_real_claude and (args.manifest or args.targets or args.list_targets):
        raise SystemExit("--self-test cannot be combined with --manifest, --targets, or --list-targets")
    if args.interactive and args.non_interactive:
        raise SystemExit("--interactive cannot be combined with --non-interactive or --yes")


def manifest_targets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    targets = manifest.get("targets") or []
    if not isinstance(targets, list) or not targets:
        raise SystemExit("manifest must contain at least one target")
    return targets


def load_targets_for_listing(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest_path = resolve_manifest_path(args.manifest, args.default_manifest)
    manifest = load_manifest(manifest_path)
    return filter_manifest_targets(manifest_targets(manifest), args.targets)


def load_batch_context(
    args: argparse.Namespace,
    *,
    make_self_test_manifest: Callable[[Path], dict[str, Any]],
) -> BatchRunContext:
    if args.self_test_real_claude:
        manifest_path = None
        out = Path(args.out).expanduser().resolve() if args.out else resolve_default_out(None, self_test=True)
        claude_bin = resolve_claude_bin(args.claude_bin)
        out.mkdir(parents=True, exist_ok=True)
        manifest = make_self_test_manifest(out)
    else:
        manifest_path = resolve_manifest_path(args.manifest, args.default_manifest)
        manifest = load_manifest(manifest_path)
        targets = manifest_targets(manifest)
        manifest["targets"] = filter_manifest_targets(targets, args.targets)
        out = Path(args.out).expanduser().resolve() if args.out else resolve_default_out(manifest_path, self_test=False)
        claude_bin = resolve_claude_bin(args.claude_bin)
        out.mkdir(parents=True, exist_ok=True)

    run_cfg = merge_cli_run_config(manifest, args)
    targets = manifest_targets(manifest)
    return BatchRunContext(
        manifest=manifest,
        manifest_path=manifest_path,
        out=out,
        claude_bin=claude_bin,
        run_cfg=run_cfg,
        targets=targets,
    )
