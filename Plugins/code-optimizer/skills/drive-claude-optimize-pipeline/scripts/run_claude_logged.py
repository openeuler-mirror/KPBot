#!/usr/bin/env python3
"""Run an interactive Claude session while recording input and output.

This is intentionally generic: it does not inspect or modify the target project.
It only runs the requested command in a PTY and writes transcript artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import termios
import tty
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\_-])"
)


def clean_terminal_text(text: str) -> str:
    """Make PTY output readable without trying to replay the terminal screen."""
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


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "project"


def default_log_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "claude-optimize-pipeline-logs"
    return Path.home() / ".codex" / "claude-optimize-pipeline-logs"


def extra_path_dirs() -> list[Path]:
    home = Path.home()
    paths = [
        home / ".local" / "bin",
        home / ".npm-global" / "bin",
        home / ".npm" / "bin",
        home / ".yarn" / "bin",
        home / ".config" / "yarn" / "global" / "node_modules" / ".bin",
        home / ".bun" / "bin",
        home / ".deno" / "bin",
        home / ".cargo" / "bin",
    ]
    for name in ("HOMEBREW_PREFIX", "LOCAL_PREFIX", "BREW_PREFIX"):
        value = os.environ.get(name)
        if value:
            paths.append(Path(value) / "bin")
    return paths


def dedupe_path(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        result.append(part)
    return result


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    current_parts = [part for part in current_path.split(os.pathsep) if part]
    extras = [str(path) for path in extra_path_dirs() if path.is_dir()]
    env["PATH"] = os.pathsep.join(dedupe_path(extras + current_parts))
    return env


def resolve_command(command: list[str], env: dict[str, str]) -> list[str]:
    if not command:
        command = ["claude"]

    executable = command[0]
    if os.sep in executable:
        return command

    if executable == "claude":
        claude_bin = os.environ.get("CLAUDE_BIN")
        if claude_bin and os.access(claude_bin, os.X_OK):
            return [claude_bin] + command[1:]

    resolved = shutil.which(executable, path=env.get("PATH"))
    if resolved:
        return [resolved] + command[1:]

    searched = env.get("PATH", "")
    raise FileNotFoundError(f"command not found: {executable}; searched PATH={searched}")


class Recorder:
    def __init__(self, log_dir: Path, meta: dict[str, object]) -> None:
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
            "# Claude Optimize Pipeline Transcript\n\n"
            f"- Started: {meta['started_at']}\n"
            f"- Project: `{meta['project_path']}`\n"
            f"- Command: `{' '.join(meta['command'])}`\n"
            f"- Log dir: `{self.log_dir}`\n\n"
        )
        self.md.write(header)
        self.clean_md.write(header)
        self.clean_md.write(
            "> Cleaned view: ANSI control sequences and redraw-only spinner lines are removed. "
            "Use `raw_terminal.log` for byte-for-byte terminal capture.\n\n"
        )
        self.md.flush()
        self.clean_md.flush()

    def write_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def event(self, kind: str, data: bytes) -> None:
        text = safe_text(data)
        record = {"ts": utc_now(), "event": kind, "text": text}
        self.events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.events.flush()
        self.md.write(f"## {record['ts']} {kind}\n\n")
        self.md.write("```text\n")
        self.md.write(fence_text(text))
        if not text.endswith("\n"):
            self.md.write("\n")
        self.md.write("```\n\n")
        self.md.flush()
        cleaned = clean_terminal_text(text)
        if cleaned:
            self.clean_md.write(f"## {record['ts']} {kind}\n\n")
            self.clean_md.write("```text\n")
            self.clean_md.write(fence_text(cleaned))
            if not cleaned.endswith("\n"):
                self.clean_md.write("\n")
            self.clean_md.write("```\n\n")
            self.clean_md.flush()

    def output(self, data: bytes) -> None:
        self.raw.write(data)
        self.raw.flush()
        self.event("claude_output", data)

    def input(self, data: bytes) -> None:
        marker = f"\n[CODEX_TO_CLAUDE {utc_now()}]\n".encode()
        self.raw.write(marker)
        self.raw.write(data)
        if not data.endswith(b"\n"):
            self.raw.write(b"\n")
        self.raw.write(b"[/CODEX_TO_CLAUDE]\n")
        self.raw.flush()
        self.event("codex_to_claude", data)

    def close(self, exit_code: int | None) -> None:
        self.meta["ended_at"] = utc_now()
        self.meta["exit_code"] = exit_code
        self.write_meta()
        self.md.write(f"## Session End\n\nExit code: `{exit_code}`\n")
        self.clean_md.write(f"## Session End\n\nExit code: `{exit_code}`\n")
        self.clean_md.close()
        self.md.close()
        self.raw.close()
        self.events.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Claude or another interactive command with transcript logging."
    )
    parser.add_argument("--project-path", required=True, help="Target project directory.")
    parser.add_argument("--log-root", default=str(default_log_root()))
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        args.command = ["claude"]
    return args


def main() -> int:
    args = parse_args()
    project_path = Path(args.project_path).expanduser().resolve()
    if not project_path.is_dir():
        print(f"project path is not a directory: {project_path}", file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.log_dir:
        log_dir = Path(args.log_dir).expanduser().resolve()
    else:
        log_dir = Path(args.log_root).expanduser().resolve() / f"{slug(project_path.name)}-{timestamp}"

    env = child_env()
    try:
        command = resolve_command(args.command, env)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 127

    meta: dict[str, object] = {
        "started_at": utc_now(),
        "ended_at": None,
        "project_path": str(project_path),
        "requested_command": args.command,
        "command": command,
        "log_dir": str(log_dir),
        "path": env.get("PATH", ""),
        "exit_code": None,
    }
    recorder = Recorder(log_dir, meta)
    print(f"[drive-claude-optimize-pipeline] transcript: {log_dir}", file=sys.stderr)

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        command,
        cwd=str(project_path),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stdin_is_tty = sys.stdin.isatty()
    old_term = None
    if stdin_is_tty:
        old_term = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    exit_code: int | None = None
    try:
        while True:
            read_fds = [master_fd]
            if stdin_is_tty:
                read_fds.append(stdin_fd)
            ready, _, _ = select.select(read_fds, [], [], 0.2)

            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    os.write(stdout_fd, data)
                    recorder.output(data)
                else:
                    break

            if stdin_is_tty and stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if data:
                    os.write(master_fd, data)
                    recorder.input(data)

            exit_code = proc.poll()
            if exit_code is not None:
                try:
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        os.write(stdout_fd, data)
                        recorder.output(data)
                except OSError:
                    pass
                break
    finally:
        if old_term is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if exit_code is None:
            try:
                exit_code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.terminate()
                exit_code = proc.wait(timeout=5)
        recorder.close(exit_code)

    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
