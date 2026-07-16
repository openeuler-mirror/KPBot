"""Logging and transcript helpers for the auto Claude PTY runner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re


ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\_-])"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def clean_terminal_text(text: str) -> str:
    text = re.sub(
        r"\x1b\[([0-9]*)C",
        lambda match: " " * min(int(match.group(1) or "1"), 80),
        text,
    )
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    cleaned: list[str] = []
    for line in text.splitlines():
        line = line.replace("\xa0", " ").strip()
        if not line:
            continue
        if len(line) == 1 and line in "✳✢✶✻✽·":
            continue
        if re.fullmatch(r"[─│┌┐└┘├┤┬┴┼╭╮╰╯━┏┓┗┛╋╂\-_= ]+", line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def fence_text(text: str) -> str:
    return text.replace("```", "` ` `")


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Recorder:
    def __init__(self, log_dir: Path, meta: dict[str, Any]) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.log_dir / "events.jsonl"
        self.raw_path = self.log_dir / "raw_terminal.log"
        self.md_path = self.log_dir / "transcript.md"
        self.clean_md_path = self.log_dir / "transcript_clean.md"
        self.meta_path = self.log_dir / "session_meta.json"
        self.events = self.events_path.open("a", encoding="utf-8")
        self.raw = self.raw_path.open("ab")
        self.md = self.md_path.open("a", encoding="utf-8")
        self.clean_md = self.clean_md_path.open("a", encoding="utf-8")
        self.meta = meta
        self.write_meta()
        header = (
            "# Auto Claude Optimize Pipeline Transcript\n\n"
            f"- Started: {meta['started_at']}\n"
            f"- Project: `{meta['project_path']}`\n"
            f"- Command: `{' '.join(meta['command'])}`\n"
            f"- Log dir: `{self.log_dir}`\n\n"
        )
        self.md.write(header)
        self.clean_md.write(header)
        self.md.flush()
        self.clean_md.flush()

    def write_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def event(self, kind: str, data: bytes | str) -> None:
        text = safe_text(data) if isinstance(data, bytes) else data
        record = {"ts": utc_now(), "event": kind, "text": text}
        self.events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.events.flush()
        self.md.write(f"## {record['ts']} {kind}\n\n```text\n{fence_text(text)}")
        if not text.endswith("\n"):
            self.md.write("\n")
        self.md.write("```\n\n")
        self.md.flush()
        cleaned = clean_terminal_text(text)
        if cleaned:
            self.clean_md.write(f"## {record['ts']} {kind}\n\n```text\n{fence_text(cleaned)}")
            if not cleaned.endswith("\n"):
                self.clean_md.write("\n")
            self.clean_md.write("```\n\n")
            self.clean_md.flush()

    def output(self, data: bytes) -> None:
        self.raw.write(data)
        self.raw.flush()
        self.event("claude_output", data)

    def input(self, text: str) -> None:
        data = text.encode("utf-8")
        marker = f"\n[AUTO_TO_CLAUDE {utc_now()}]\n".encode()
        self.raw.write(marker)
        self.raw.write(data)
        if not data.endswith(b"\n"):
            self.raw.write(b"\n")
        self.raw.write(b"[/AUTO_TO_CLAUDE]\n")
        self.raw.flush()
        self.event("auto_to_claude", text)

    def close(self, status: str, exit_code: int | None, error: str | None = None) -> None:
        self.meta["ended_at"] = utc_now()
        self.meta["exit_code"] = exit_code
        self.meta["status"] = status
        self.meta["error"] = error
        self.write_meta()
        self.md.write(f"## Session End\n\nStatus: `{status}`\n\nExit code: `{exit_code}`\n")
        if error:
            self.md.write(f"\nError: {error}\n")
        self.clean_md.write(f"## Session End\n\nStatus: `{status}`\n\nExit code: `{exit_code}`\n")
        if error:
            self.clean_md.write(f"\nError: {error}\n")
        self.clean_md.close()
        self.md.close()
        self.raw.close()
        self.events.close()
