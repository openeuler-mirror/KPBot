"""Top-level batch pipeline orchestration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from config_loader import BatchRunContext
from result_collector import SUCCESS_STATUSES
from task_scheduler import TaskScheduler


class BatchPipeline:
    def __init__(
        self,
        *,
        context: BatchRunContext,
        legacy_driver: bool,
        managed_driver_version: str,
        run_target: Callable[..., dict[str, Any]],
        write_summary: Callable[[Path, list[dict[str, Any]]], None],
        progress_renderer: Callable[[Path, list[dict[str, Any]], str | None], None],
        now: Callable[[], str],
    ) -> None:
        self.context = context
        self.legacy_driver = legacy_driver
        self.managed_driver_version = managed_driver_version
        self.run_target = run_target
        self.write_summary = write_summary
        self.progress_renderer = progress_renderer
        self.now = now

    def run(self) -> int:
        scheduler = TaskScheduler(
            targets=self.context.targets,
            out=self.context.out,
            run_cfg=self.context.run_cfg,
            legacy_driver=self.legacy_driver,
            managed_driver_version=self.managed_driver_version,
            progress_renderer=self.progress_renderer,
            now=self.now,
            failure_exceptions=(
                RuntimeError,
                subprocess.SubprocessError,
                FileNotFoundError,
                OSError,
                json.JSONDecodeError,
            ),
        )
        results = scheduler.run_serial(
            lambda target, progress_callback: self.run_target(
                target,
                self.context.out,
                self.context.run_cfg,
                self.context.claude_bin,
                legacy_driver=self.legacy_driver,
                progress_callback=progress_callback,
            )
        )

        self.write_summary(self.context.out, results)
        self.progress_renderer(self.context.out, self.context.targets, None)
        print(f"\n[batch] summary written: {self.context.out / 'summary.md'}", flush=True)
        return 0 if all(item.get("status") in SUCCESS_STATUSES for item in results) else 1
