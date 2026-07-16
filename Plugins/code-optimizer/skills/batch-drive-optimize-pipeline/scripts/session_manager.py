"""Session state setup for the auto Claude PTY runner."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from log_handler import Recorder, utc_now, write_json_atomic


class SessionManager:
    def __init__(self, project_path: Path, log_dir: Path, command: list[str], args: Namespace) -> None:
        self.project_path = project_path
        self.log_dir = log_dir
        self.command = command
        self.args = args
        self.started_at = utc_now()
        self.meta = self._build_meta()

    def _build_meta(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": None,
            "project_path": str(self.project_path),
            "command": self.command,
            "log_dir": str(self.log_dir),
            "timeout_seconds": self.args.timeout_seconds,
            "idle_timeout_seconds": self.args.idle_timeout_seconds,
            "max_prompt_repeats": self.args.max_prompt_repeats,
            "attempt_index": self.args.attempt_index,
            "context_soft_limit_tokens": self.args.context_soft_limit_tokens,
            "context_hard_limit_tokens": self.args.context_hard_limit_tokens,
            "status": None,
            "error": None,
        }

    def recorder(self) -> Recorder:
        return Recorder(self.log_dir, self.meta)

    def write_running_result(self) -> None:
        write_json_atomic(
            self.log_dir / "auto_result.json",
            {
                "status": "running",
                "error": None,
                "exit_code": None,
                "log_dir": str(self.log_dir),
                "final_marker_seen": False,
                "reply_counts": {},
                "command": self.command,
                "started_at": self.started_at,
                "updated_at": utc_now(),
            },
        )
