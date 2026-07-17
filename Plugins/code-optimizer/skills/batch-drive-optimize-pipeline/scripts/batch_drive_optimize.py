#!/usr/bin/env python3
"""Batch-drive real Claude through /kpbot-code-optimizer targets."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ability_evidence import write_batch_evidence  # noqa: E402
from batch_runtime import DEFAULT_RETRY_CONFIG, read_limited, retry_call, safe_file_size  # noqa: E402
from config_loader import (  # noqa: E402
    DEFAULT_MANIFEST,
    load_batch_context,
    load_targets_for_listing,
    resolve_claude_bin,
    resolve_default_out,
    resolve_manifest_path,
    timestamp_for_path,
    validate_args,
)
from create_virtual_project import create_project  # noqa: E402
from data_loader import parse_simple_yaml, simple_scalar  # noqa: E402
from pipeline import BatchPipeline  # noqa: E402
from result_collector import SUCCESS_STATUSES, collect_target_result  # noqa: E402
from stage_executor import StageExecutor, StageExecutorConfig  # noqa: E402
from task_scheduler import filter_manifest_targets, print_target_list, slug, target_manifest_id  # noqa: E402


DEFAULT_TIMEOUT_MINUTES = 180
DEFAULT_IDLE_TIMEOUT_MINUTES = 10
DEFAULT_RESUME_ATTEMPTS = 2
DEFAULT_PROGRESS_INTERVAL_SECONDS = 30
DEFAULT_CONTEXT_SOFT_LIMIT_TOKENS = 0
DEFAULT_CONTEXT_HARD_LIMIT_TOKENS = 0
DEFAULT_RESUME_PROMPT_MAX_TOKENS = 0
DEFAULT_TRANSCRIPT_TAIL_LINES = 120
MANAGED_DRIVER_VERSION = "managed-batch-v1"
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120
DEFAULT_PROBE_TIMEOUT_SECONDS = 8
TERMINAL_PROGRESS_STATUSES = {
    "failed",
    "semantic_idle_timeout",
    "context_limit",
    "context_rollover",
    "timeout",
}


def optional_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def config_int(target: dict[str, Any], run_cfg: dict[str, Any], key: str, default: int) -> int:
    value = target.get(key)
    if value is None or value == "":
        value = run_cfg.get(key)
    return optional_int(value, default)

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "node_modules",
    ".venv",
    "venv",
}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".s", ".S"}
PERF_NAME_RE = re.compile(
    r"(sgemm|gemm|matmul|conv|fft|stencil|sort|hash|compress|crypto|memcpy|memmove|"
    r"kernel|microkernel|dot|sum|scan|vec|vector|neon|sve|sme)",
    re.I,
)
FUNC_DEF_RE = re.compile(
    r"^\s*(?:static\s+|inline\s+|extern\s+|__attribute__\s*\(\([^)]*\)\)\s*)*"
    r"(?:[A-Za-z_][\w\s\*\(\),]*\s+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{"
)
FUNC_SKIP_NAMES = {"if", "for", "while", "switch", "return", "sizeof"}
MAX_FUNC_DEF_LINE_CHARS = 512
TOKEN_ESTIMATE_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]", re.UNICODE)
_TOKEN_ENCODER: Any | None = None
_TOKEN_METHOD: str | None = None
PHASE_LABELS = {
    "starting": "启动 worker",
    "claude_auto_update": "Claude 自动更新",
    "project_preparation": "准备项目环境",
    "hotspot_analysis": "热点分析",
    "optimization_point": "优化点处理",
    "verification": "验证结果",
    "final_wrap_up": "输出最终报告",
    "pipeline_complete": "全流程完成",
}
BATCH_ALLOWED_BASH_COMMANDS = [
    "mkdir",
    "git",
    "grep",
    "rg",
    "find",
    "ls",
    "cat",
    "head",
    "tail",
    "make",
    "cmake",
    "cc",
    "gcc",
    "g++",
    "clang",
    "perf",
    "taskset",
    "python",
    "python3",
    "jq",
    "sed",
    "awk",
    "tr",
    "sort",
    "uniq",
    "wc",
    "diff",
    "patch",
    "cp",
    "mv",
    "rm",
    "touch",
    "chmod",
    "date",
    "uname",
    "which",
    "whereis",
    "ctest",
    "ninja",
    "ld",
    "nm",
    "objdump",
    "readelf",
    "file",
    "strings",
    "timeout",
    "time",
    "tee",
    "xargs",
    "env",
    "printenv",
    "dirname",
    "basename",
    "realpath",
    "pwd",
    "cd",
    "echo",
    "printf",
    "stat",
    "md5sum",
    "sha256sum",
    "lscpu",
    "arm_query.py",
    "query.py",
]
BATCH_ALLOWED_PERMISSION_RULES = [f"Bash({cmd}:*)" for cmd in BATCH_ALLOWED_BASH_COMMANDS] + [
    "Bash(cat /proc:*)",
    "Bash(./test:*)",
    "Bash(./perf:*)",
    "Bash(./bench:*)",
]


def run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout_seconds: int = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )

    try:
        proc = retry_call(
            invoke,
            retry_exceptions=(subprocess.TimeoutExpired,),
            config=DEFAULT_RETRY_CONFIG,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        proc = subprocess.CompletedProcess(cmd, 124, stdout=stdout or f"command timed out after {timeout_seconds} seconds\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc


def run_probe(command: list[str], cwd: Path, timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS) -> tuple[int, str]:
    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )

    try:
        proc = retry_call(
            invoke,
            retry_exceptions=(subprocess.TimeoutExpired,),
            config=DEFAULT_RETRY_CONFIG,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 124, str(exc)
    return proc.returncode, proc.stdout


def run_streamed(
    cmd: list[str],
    cwd: Path | None = None,
    progress_callback: Callable[[], None] | None = None,
    progress_interval_seconds: float = 30.0,
    timeout_seconds: int | None = None,
) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    stdout_tail = ""
    last_progress = 0.0
    assert proc.stdout is not None
    stdout_pipe = proc.stdout
    stdout_fd = stdout_pipe.fileno()

    def append_stdout(data: bytes) -> None:
        nonlocal stdout_tail
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        print(text, end="", flush=True)
        stdout_tail = (stdout_tail + text)[-12000:]

    def drain_after_exit() -> None:
        while True:
            try:
                data = os.read(stdout_fd, 8192)
            except OSError:
                return
            if not data:
                return
            append_stdout(data)

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while True:
        now = time.monotonic()
        if proc.poll() is not None:
            drain_after_exit()
            stdout_pipe.close()
            return proc.returncode or 0, stdout_tail
        if deadline is not None and now >= deadline:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            drain_after_exit()
            message = f"\n[batch-driver] streamed process timed out after {timeout_seconds} seconds\n"
            print(message, end="", flush=True)
            stdout_pipe.close()
            return 124, (stdout_tail + message)[-12000:]
        if progress_callback and now - last_progress >= progress_interval_seconds:
            progress_callback()
            last_progress = now
        ready, _, _ = select.select([stdout_pipe], [], [], 0.2)
        if stdout_pipe in ready:
            try:
                data = os.read(stdout_fd, 8192)
            except OSError:
                data = b""
            if data:
                append_stdout(data)
            elif proc.poll() is not None:
                stdout_pipe.close()
                return proc.returncode or 0, stdout_tail


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def estimate_text_tokens(text: str) -> tuple[int, str, bool]:
    global _TOKEN_ENCODER, _TOKEN_METHOD
    if _TOKEN_METHOD is None:
        try:
            import tiktoken  # type: ignore

            _TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
            _TOKEN_METHOD = "tiktoken_cl100k_base_estimate"
        except ImportError:
            _TOKEN_ENCODER = None
            _TOKEN_METHOD = "heuristic_word_cjk_punct_estimate"
    if _TOKEN_ENCODER is not None:
        return len(_TOKEN_ENCODER.encode(text, disallowed_special=())), str(_TOKEN_METHOD), True
    return sum(1 for _ in TOKEN_ESTIMATE_RE.finditer(text)), str(_TOKEN_METHOD), True


def display_width(value: str) -> int:
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_cell(value: Any, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - display_width(text))


def render_box_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    table_rows = [headers, *rows]
    widths = [max(display_width(row[idx]) for row in table_rows) for idx in range(len(headers))]
    top = "┌" + "┬".join("─" * (width + 2) for width in widths) + "┐"
    sep = "├" + "┼".join("─" * (width + 2) for width in widths) + "┤"
    bottom = "└" + "┴".join("─" * (width + 2) for width in widths) + "┘"
    lines = [top]
    lines.append("│ " + " │ ".join(pad_cell(headers[idx], widths[idx]) for idx in range(len(headers))) + " │")
    lines.append(sep)
    for row in rows:
        lines.append("│ " + " │ ".join(pad_cell(row[idx], widths[idx]) for idx in range(len(headers))) + " │")
    lines.append(bottom)
    return lines


def format_number(value: int | None) -> str:
    return f"{value:,}" if value is not None else "-"


def format_transcript_lines(line_count: int | None) -> str:
    return f"{line_count:,} 行" if line_count is not None else "-"


def format_tokens(token_count: int | None, estimated: bool) -> str:
    if token_count is None:
        return "-"
    suffix = " 估" if estimated else ""
    return f"{token_count:,}{suffix}"


def format_elapsed(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def count_lines_and_tokens(path: Path) -> tuple[int | None, int | None]:
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    token_count = sum(1 for _ in TOKEN_ESTIMATE_RE.finditer(text))
    return line_count, token_count


def token_estimate_from_bytes(byte_count: int) -> int:
    if byte_count <= 0:
        return 0
    return max(1, (byte_count + 3) // 4)


def transcript_metadata_stats(path: Path) -> tuple[int | None, int | None, bool]:
    try:
        stat = path.stat()
    except OSError:
        return None, None, True
    return None, token_estimate_from_bytes(stat.st_size), True


def live_transcript_stats(target_out: Path) -> tuple[int | None, int | None, bool]:
    target_result = read_json_file(target_out / "target_result.json")
    token_stats = target_result.get("transcript_token_stats")
    if isinstance(token_stats, dict):
        token_count = int(token_stats.get("token_count") or 0)
        return None, token_count, bool(token_stats.get("is_estimate", True))

    token_file = read_json_file(target_out / "transcript_tokens.json")
    if token_file:
        return None, int(token_file.get("token_count") or 0), bool(token_file.get("is_estimate", True))

    for transcript_path in [target_out / "transcript_clean.md", target_out / "claude_log" / "transcript_clean.md"]:
        line_count, token_count, estimated = transcript_metadata_stats(transcript_path)
        if line_count is not None or token_count is not None:
            return line_count, token_count, estimated
    return None, None, True


def usage_summary_from_file(target_out: Path) -> dict[str, Any]:
    usage = read_json_file(target_out / "usage.json")
    if usage:
        return {
            "worker_input_tokens_estimate": usage.get("worker_input_tokens_estimate"),
            "worker_output_tokens_estimate": usage.get("worker_output_tokens_estimate"),
            "transcript_tokens_estimate": usage.get("transcript_tokens_estimate"),
            "driver_prompt_tokens_estimate": usage.get("driver_prompt_tokens_estimate"),
            "context_window_peak_estimate": usage.get("context_window_peak_estimate"),
            "is_exact": usage.get("is_exact", False),
        }
    _, token_count, estimated = live_transcript_stats(target_out)
    return {
        "worker_input_tokens_estimate": None,
        "worker_output_tokens_estimate": None,
        "transcript_tokens_estimate": token_count,
        "driver_prompt_tokens_estimate": None,
        "context_window_peak_estimate": None,
        "is_exact": not estimated if token_count is not None else False,
    }


def timing_summary_from_file(target_out: Path) -> dict[str, Any]:
    timing = read_json_file(target_out / "timing.json")
    if not timing:
        return {}
    return {
        "wall_time_seconds": timing.get("wall_time_seconds"),
        "active_worker_seconds": timing.get("active_worker_seconds"),
        "driver_overhead_seconds": timing.get("driver_overhead_seconds"),
        "attempt_count": timing.get("attempt_count"),
    }


def current_stage_label(progress: dict[str, Any], completed: bool, failed: bool) -> str:
    if completed:
        return "全流程完成"
    if failed:
        return "失败或未通过收集门控"
    phase = str(progress.get("phase") or "")
    label = PHASE_LABELS.get(phase, phase or "等待启动")
    elapsed = progress.get("elapsed_seconds")
    elapsed_text = format_elapsed(int(elapsed)) if isinstance(elapsed, int) else ""
    return f"{label}（约 {elapsed_text}）" if elapsed_text else label


def task_counts_text(counts: Any) -> str:
    if not isinstance(counts, dict):
        return "-"
    total = counts.get("total")
    done = counts.get("done")
    in_progress = counts.get("in_progress")
    pending = counts.get("pending")
    if total is None:
        return "-"
    parts = [f"{done or 0}/{total} done"]
    if in_progress is not None:
        parts.append(f"{in_progress} running")
    if pending is not None:
        parts.append(f"{pending} pending")
    return ", ".join(parts)


def reply_counts_text(reply_counts: Any) -> str:
    if not isinstance(reply_counts, dict) or not reply_counts:
        return "-"
    items: list[tuple[str, int]] = []
    for key, value in reply_counts.items():
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            items.append((str(key), count))
    if not items:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(items))


def stage_digest(stages: Any, limit: int = 5) -> str:
    if not isinstance(stages, list) or not stages:
        return "-"
    parts: list[str] = []
    for item in stages[-limit:]:
        if isinstance(item, dict):
            stage = str(item.get("stage") or "").strip()
            status = str(item.get("status") or "").strip()
            if stage:
                parts.append(f"{stage}:{status or '?'}")
    return "; ".join(parts) if parts else "-"


def points_digest(points: Any, limit: int = 3) -> str:
    if not isinstance(points, list) or not points:
        return "-"
    return "; ".join(str(point).strip() for point in points[-limit:] if str(point).strip()) or "-"


def patch_bytes(target_out: Path, target_result: dict[str, Any]) -> int:
    patch_info = target_result.get("patch_info")
    if isinstance(patch_info, dict):
        return int(patch_info.get("final_patch_bytes") or 0)
    patch_path = target_out / "final.patch"
    try:
        return patch_path.stat().st_size
    except OSError:
        return 0


def internal_commit_count(target_out: Path, target_result: dict[str, Any]) -> int:
    commits = target_result.get("internal_commits")
    if isinstance(commits, list):
        return len([item for item in commits if str(item).strip()])
    log_path = target_out / "internal_git_log.txt"
    if not log_path.exists():
        return 0
    return len([line for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])


def completion_status_from_result(target_result: dict[str, Any]) -> str:
    explicit = target_result.get("completion_status")
    if explicit:
        return str(explicit)
    if target_result.get("reached_final_summary"):
        return "completed"
    runner = target_result.get("legacy_runner_status")
    if not runner and isinstance(target_result.get("runner"), dict):
        runner = target_result["runner"].get("status")
    return str(runner or "")


def current_status_labels(target_result: dict[str, Any], active: bool) -> tuple[str, str, str]:
    quality_status = str(target_result.get("quality_status") or target_result.get("status") or "")
    completion_status = completion_status_from_result(target_result)
    if quality_status in SUCCESS_STATUSES:
        return quality_status, completion_status or "completed", "✅ 完成"
    if completion_status == "completed":
        return quality_status, completion_status, "⚠ 完成待审"
    if quality_status:
        return quality_status, completion_status or "incomplete", "❌ 失败"
    if active:
        return "", "running", "⏳ 进行中"
    return "", "waiting", "◻ 等待"


def progress_status_labels(progress: dict[str, Any]) -> tuple[str, str, str] | None:
    progress_status = str(progress.get("status") or "")
    if not progress_status or progress_status == "running":
        return None
    if progress_status in TERMINAL_PROGRESS_STATUSES:
        return progress_status, progress_status, "❌ 失败"
    return "", progress_status, "◻ 等待"


def target_has_live_progress(target_out: Path) -> bool:
    if (target_out / "target_result.json").exists():
        return False
    progress = read_json_file(target_out / "progress.json")
    progress_status = str(progress.get("status") or "")
    if progress_status in TERMINAL_PROGRESS_STATUSES:
        return False
    target_state = read_json_file(target_out / "target_state.json")
    return progress_status == "running" or target_state.get("run_status") in {"initializing", "running", "resuming", "collecting"}


def file_status(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def target_status_snapshot(out: Path, target_id: str, active_target_id: str | None) -> dict[str, Any]:
    target_out = out / "targets" / target_id
    progress = read_json_file(target_out / "progress.json")
    target_result = read_json_file(target_out / "target_result.json")
    auto_result = read_json_file(target_out / "claude_log" / "auto_result.json")
    target_state = read_json_file(target_out / "target_state.json")
    usage = usage_summary_from_file(target_out)
    timing = timing_summary_from_file(target_out)
    active = target_id == active_target_id and target_has_live_progress(target_out)
    quality_status, completion_status, status_label = current_status_labels(target_result, active)
    progress_labels = progress_status_labels(progress) if not target_result else None
    if progress_labels:
        quality_status, completion_status, status_label = progress_labels
    elif not target_result and progress:
        completion_status = str(progress.get("status") or completion_status or "running")
    stage_label = current_stage_label(
        progress,
        quality_status in SUCCESS_STATUSES,
        bool(quality_status and quality_status not in SUCCESS_STATUSES and completion_status != "completed"),
    )
    elapsed = progress.get("elapsed_seconds")
    idle = progress.get("idle_seconds")
    stages = progress.get("stages")
    points = progress.get("optimization_points")
    task_counts = progress.get("task_counts")
    reply_counts = progress.get("reply_counts") or auto_result.get("reply_counts")
    message = " ".join(str(progress.get("message") or auto_result.get("error") or "").split())[:220]
    log_files: dict[str, dict[str, Any]] = {}
    for name in [
        "progress.json",
        "target_state.json",
        "attempts.jsonl",
        "usage.json",
        "timing.json",
        "completion_gate.json",
        "patch_manifest.json",
        "target_result.json",
        "transcript_clean.md",
        "transcript.md",
        "events.jsonl",
        "raw_terminal.log",
    ]:
        candidates = [target_out / name, target_out / "claude_log" / name]
        for candidate in candidates:
            meta = file_status(candidate)
            if meta:
                log_files[name] = meta
                break
    artifact_names = sorted(log_files)
    human_summary = {
        "line": (
            f"{target_id}: {status_label} | {stage_label} | "
            f"elapsed={format_elapsed(int(elapsed)) if isinstance(elapsed, int) else '-'} "
            f"idle={format_elapsed(int(idle)) if isinstance(idle, int) else '-'} | "
            f"tasks={task_counts_text(task_counts)} | replies={reply_counts_text(reply_counts)}"
        ),
        "stage_label": stage_label,
        "elapsed": format_elapsed(int(elapsed)) if isinstance(elapsed, int) else "-",
        "idle": format_elapsed(int(idle)) if isinstance(idle, int) else "-",
        "tasks": task_counts_text(task_counts),
        "stage_digest": stage_digest(stages),
        "optimization_points": points_digest(points),
        "auto_replies": reply_counts_text(reply_counts),
        "message": message or None,
        "artifacts": artifact_names,
    }
    return {
        "target_id": target_id,
        "active": active,
        "status_label": status_label,
        "quality_status": quality_status or None,
        "completion_status": completion_status or None,
        "reached_final_summary": target_result.get("reached_final_summary"),
        "legacy_runner_status": target_result.get("legacy_runner_status"),
        "progress": {
            "status": progress.get("status"),
            "phase": progress.get("phase"),
            "updated_at": progress.get("updated_at"),
            "elapsed_seconds": progress.get("elapsed_seconds"),
            "idle_seconds": progress.get("idle_seconds"),
            "final_marker_seen": progress.get("final_marker_seen"),
            "stages": progress.get("stages"),
            "optimization_points": progress.get("optimization_points"),
            "task_counts": progress.get("task_counts"),
            "reply_counts": progress.get("reply_counts"),
            "message": progress.get("message"),
        },
        "auto_result": {
            "status": auto_result.get("status"),
            "updated_at": auto_result.get("updated_at"),
            "started_at": auto_result.get("started_at"),
            "ended_at": auto_result.get("ended_at"),
            "exit_code": auto_result.get("exit_code"),
            "final_marker_seen": auto_result.get("final_marker_seen"),
            "error": auto_result.get("error"),
        },
        "target_state": {
            "run_status": target_state.get("run_status"),
            "pipeline_status": target_state.get("pipeline_status"),
            "quality_status": target_state.get("quality_status"),
            "current_attempt": target_state.get("current_attempt"),
            "updated_at": target_state.get("updated_at"),
        },
        "usage": usage,
        "timing": timing,
        "human_summary": human_summary,
        "artifacts": log_files,
        "paths": {
            "target_out": str(target_out),
            "progress": str(target_out / "progress.json"),
            "target_result": str(target_out / "target_result.json"),
        },
    }


def write_status_snapshot(out: Path, targets: list[dict[str, Any]], active_target_id: str | None = None) -> dict[str, Any]:
    target_ids = [target_manifest_id(target) for target in targets]
    snapshots = [target_status_snapshot(out, target_id, active_target_id) for target_id in target_ids]
    completion_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    for item in snapshots:
        completion = str(item.get("completion_status") or "unknown")
        quality = str(item.get("quality_status") or item.get("completion_status") or "unknown")
        completion_counts[completion] = completion_counts.get(completion, 0) + 1
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
    active_snapshot = next((item for item in snapshots if item.get("active")), None)
    snapshot = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output_dir": str(out),
        "active_target_id": active_target_id,
        "counts": {
            "total": len(snapshots),
            "completion": completion_counts,
            "quality": quality_counts,
        },
        "human_summary": {
            "active": (active_snapshot or {}).get("human_summary", {}).get("line"),
            "targets": [item.get("human_summary", {}).get("line") for item in snapshots],
        },
        "targets": snapshots,
    }
    (out / "status_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def infer_monitor_targets(out: Path) -> list[dict[str, Any]]:
    targets_root = out / "targets"
    if not targets_root.exists():
        return []
    targets: list[dict[str, Any]] = []
    for target_dir in sorted(targets_root.iterdir()):
        if target_dir.is_dir():
            targets.append({"id": target_dir.name, "project_path": ""})
    return targets


def infer_active_target_id(out: Path, targets: list[dict[str, Any]]) -> str | None:
    existing_snapshot = read_json_file(out / "status_snapshot.json")
    existing_active = existing_snapshot.get("active_target_id")
    if existing_active:
        existing_target_out = out / "targets" / slug(str(existing_active))
        if target_has_live_progress(existing_target_out):
            return str(existing_active)
    for target in targets:
        target_id = target_manifest_id(target)
        if target_has_live_progress(out / "targets" / target_id):
            return target_id
    return None


def monitor_status_snapshot(out: Path) -> dict[str, Any]:
    targets = infer_monitor_targets(out)
    if targets:
        return write_status_snapshot(out, targets, infer_active_target_id(out, targets))
    snapshot = read_json_file(out / "status_snapshot.json")
    if snapshot:
        return snapshot
    raise SystemExit(f"no batch status found under: {out}")


def render_monitor_snapshot(snapshot: dict[str, Any]) -> str:
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
    completion = counts.get("completion") if isinstance(counts.get("completion"), dict) else {}
    quality = counts.get("quality") if isinstance(counts.get("quality"), dict) else {}
    human = snapshot.get("human_summary") if isinstance(snapshot.get("human_summary"), dict) else {}
    target_lines = human.get("targets") if isinstance(human.get("targets"), list) else []
    lines = [
        "Bounded batch monitor snapshot",
        f"Output: {snapshot.get('output_dir') or '-'}",
        f"Active: {snapshot.get('active_target_id') or '-'}",
        f"Completion counts: {json.dumps(completion, ensure_ascii=False, sort_keys=True)}",
        f"Quality counts: {json.dumps(quality, ensure_ascii=False, sort_keys=True)}",
        "",
        "Targets:",
    ]
    if target_lines:
        lines.extend(f"- {line}" for line in target_lines if line)
    else:
        lines.append("- no target summaries available")
    lines.extend(
        [
            "",
            "Transcript bodies intentionally omitted. Use small tails only when debugging a specific active target.",
        ]
    )
    return "\n".join(lines)


def target_overview(out: Path, target_id: str, active_target_id: str | None) -> dict[str, Any]:
    target_out = out / "targets" / target_id
    progress = read_json_file(target_out / "progress.json")
    target_result = read_json_file(target_out / "target_result.json")
    auto_result = read_json_file(target_out / "claude_log" / "auto_result.json")
    status, completion_status, status_label = current_status_labels(
        target_result,
        target_id == active_target_id and target_has_live_progress(target_out),
    )
    progress_labels = progress_status_labels(progress) if not target_result else None
    if progress_labels:
        status, completion_status, status_label = progress_labels
    completed = status in SUCCESS_STATUSES
    completion_with_quality_issues = completion_status == "completed" and not completed
    failed = bool(status and not completed and not completion_with_quality_issues)
    running = target_id == active_target_id and not status and completion_status == "running"
    waiting = not running and not status and completion_status != "running"
    line_count, token_count, estimated_tokens = live_transcript_stats(target_out)
    usage = usage_summary_from_file(target_out)
    timing = timing_summary_from_file(target_out)
    if usage.get("transcript_tokens_estimate") is not None:
        token_count = int(usage.get("transcript_tokens_estimate") or 0)
        estimated_tokens = not bool(usage.get("is_exact"))

    return {
        "target_id": target_id,
        "status": status,
        "completion_status": completion_status,
        "status_label": status_label,
        "line_count": line_count,
        "token_count": token_count,
        "estimated_tokens": estimated_tokens,
        "stage": "全流程完成（质量门控未通过）" if completion_with_quality_issues else current_stage_label(progress, completed, failed),
        "phase": progress.get("phase"),
        "elapsed_seconds": progress.get("elapsed_seconds"),
        "idle_seconds": progress.get("idle_seconds"),
        "task_counts": progress.get("task_counts"),
        "stages": progress.get("stages"),
        "optimization_points": progress.get("optimization_points"),
        "reply_counts": progress.get("reply_counts") or auto_result.get("reply_counts"),
        "message": progress.get("message") or auto_result.get("error"),
        "patch_bytes": patch_bytes(target_out, target_result),
        "commit_count": internal_commit_count(target_out, target_result),
        "wall_time_seconds": timing.get("wall_time_seconds"),
        "attempt_count": timing.get("attempt_count"),
    }


def render_batch_progress(out: Path, targets: list[dict[str, Any]], active_target_id: str | None = None) -> str:
    rows_data = [target_overview(out, target_manifest_id(target), active_target_id) for target in targets]
    rows = [
        [
            str(item["target_id"]),
            str(item["status_label"]),
            str(item["stage"]),
            task_counts_text(item.get("task_counts")),
            (
                f"{format_elapsed(int(item['elapsed_seconds'])) if isinstance(item.get('elapsed_seconds'), int) else '-'} / "
                f"{format_elapsed(int(item['idle_seconds'])) if isinstance(item.get('idle_seconds'), int) else '-'}"
            ),
            reply_counts_text(item.get("reply_counts")),
            format_tokens(item["token_count"], bool(item["estimated_tokens"])),
        ]
        for item in rows_data
    ]
    total_tokens = sum(int(item["token_count"] or 0) for item in rows_data)
    lines = [
        "",
        "进度总览：",
        "",
        *render_box_table(["目标", "状态", "当前阶段", "任务", "耗时/空闲", "自动回复", "Tokens"], rows),
    ]
    lines.append("")
    lines.append(f"Token 消耗估算：{total_tokens:,} tokens")

    active_items = [item for item in rows_data if item["target_id"] == active_target_id]
    if active_items:
        item = active_items[0]
        lines.append("")
        lines.append("当前运行目标：")
        lines.append(f"- 目标: {item['target_id']}")
        lines.append(f"- 阶段: {item['stage']} ({item.get('phase') or 'phase unknown'})")
        lines.append(f"- 任务: {task_counts_text(item.get('task_counts'))}")
        lines.append(f"- 阶段记录: {stage_digest(item.get('stages'))}")
        lines.append(f"- 优化点: {points_digest(item.get('optimization_points'))}")
        lines.append(f"- 自动回复: {reply_counts_text(item.get('reply_counts'))}")
        if item.get("message"):
            lines.append(f"- 最近消息: {' '.join(str(item['message']).split())[:220]}")

    completed = [item for item in rows_data if item["status"] in SUCCESS_STATUSES]
    if completed:
        lines.append("")
        lines.append("已完成目标成果：")
        for item in completed:
            patch_status = "✅" if int(item["patch_bytes"] or 0) > 0 else "无 patch"
            commit_status = "✅" if int(item["commit_count"] or 0) > 0 else "无提交"
            lines.append(f"- {item['target_id']}: Patch {format_number(item['patch_bytes'])} bytes {patch_status}; Git 提交 {commit_status}; Tokens {format_tokens(item['token_count'], bool(item['estimated_tokens']))}")
    lines.append("")
    return "\n".join(lines)


def print_batch_progress(out: Path, targets: list[dict[str, Any]], active_target_id: str | None = None) -> None:
    try:
        write_status_snapshot(out, targets, active_target_id)
    except OSError as exc:
        print(f"[batch-progress] warning: could not write status snapshot: {exc}", flush=True)
    print(render_batch_progress(out, targets, active_target_id), flush=True)


def copy_project(source: Path, dest: Path) -> None:
    source = source.expanduser().resolve()
    dest = dest.expanduser().resolve()
    if dest.exists():
        shutil.rmtree(dest)

    ignore = shutil.ignore_patterns(
        ".git",
        "optimization_reports",
        "__pycache__",
        "*.o",
        "*.tmp",
        "perf.data",
        "perf.data.old",
        "core.*",
    )
    shutil.copytree(source, dest, ignore=ignore)
    run(["git", "init"], dest)
    run(["git", "config", "user.email", "batch-drive@example.invalid"], dest)
    run(["git", "config", "user.name", "Batch Drive Optimize"], dest)
    run(["git", "add", "-A"], dest)
    run(["git", "commit", "-m", "batch baseline copy"], dest)
    run(["git", "tag", "batch_baseline"], dest)


def has_optimize_pipeline_skill(skills_dir: Path) -> bool:
    return (skills_dir / "kpbot-code-optimizer" / "SKILL.md").is_file()


def pipeline_skills_candidates(source_project: Path) -> list[Path]:
    candidates: list[Path] = []
    env_dir = os.environ.get("KUNPENG_PIPELINE_SKILLS_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.extend(
        [
            source_project.parent / ".claude" / "skills",
            Path.cwd() / ".claude" / "skills",
            SCRIPT_DIR.parent.parent,
            source_project / ".claude" / "skills",
        ]
    )

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def find_pipeline_skills_dir(source_project: Path) -> Path | None:
    for candidate in pipeline_skills_candidates(source_project):
        if has_optimize_pipeline_skill(candidate):
            return candidate
    return None


def remove_path(path: Path) -> None:
    try:
        path.unlink()
    except IsADirectoryError:
        shutil.rmtree(path)
    except PermissionError:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            raise
    except FileNotFoundError:
        pass


def ensure_workdir_pipeline_skills(workdir: Path, source_project: Path) -> Path | None:
    """Install a current pipeline skill tree into the temporary worker copy."""
    dest = workdir / ".claude" / "skills"
    source = find_pipeline_skills_dir(source_project)
    if source is None:
        if has_optimize_pipeline_skill(dest):
            return None
        raise RuntimeError(
            "pipeline skills not found for worker workdir; install .claude/skills or set KUNPENG_PIPELINE_SKILLS_DIR"
        )

    try:
        if dest.resolve(strict=True) == source.resolve(strict=True) and has_optimize_pipeline_skill(dest):
            return source
    except OSError:
        pass
    try:
        remove_path(dest)
    except OSError as exc:
        raise RuntimeError(f"failed to replace worker pipeline skills at {dest}: {exc}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(source, dest, target_is_directory=True)
    except OSError:
        shutil.copytree(source, dest)
    return source


def project_status(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    return run(["git", "status", "--short"], path, check=False).stdout


def is_under_skipped_dir(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def match_function_definition(line: str) -> re.Match[str] | None:
    if len(line) > MAX_FUNC_DEF_LINE_CHARS:
        return None
    if "(" not in line or ")" not in line or "{" not in line:
        return None
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("#", "//", "/*", "*")):
        return None
    return FUNC_DEF_RE.match(line)


def list_project_files(project: Path, max_files: int = 8000) -> list[Path]:
    files: list[Path] = []
    stack = [project]
    while stack:
        root = stack.pop()
        try:
            entries = os.scandir(root)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if entry.name in SKIP_DIRS:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and not entry.name.endswith(".tmp"):
                        files.append(Path(entry.path))
                        if len(files) >= max_files:
                            return files
                except OSError:
                    continue
    return files


def parse_make_targets(makefile: Path) -> set[str]:
    text = read_limited(makefile)
    targets: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith(("\t", " ")):
            continue
        match = re.match(r"^([A-Za-z0-9_.%/-]+)\s*:(?!=)", line)
        if not match:
            continue
        name = match.group(1)
        if "%" in name or "/" in name or name.startswith("."):
            continue
        targets.add(name)
    return targets


def discover_ctest_candidates(project: Path, files: list[Path]) -> tuple[list[str], list[str], list[str]]:
    test_tools: list[str] = []
    test_commands: list[str] = []
    bench_commands: list[str] = []
    build_dirs = sorted({path.parent for path in files if path.name in {"CTestTestfile.cmake", "CMakeCache.txt"}})
    for build_dir in build_dirs[:4]:
        code, output = run_probe(["ctest", "-N"], build_dir)
        if code != 0 or not output.strip():
            continue
        rel_build = relative(build_dir, project)
        test_names = re.findall(r"Test\s*#?\d*:\s*([^\n]+)", output)
        test_tools.append(f"ctest in {rel_build}")
        if test_names:
            joined = "|".join(re.escape(name.strip()) for name in test_names[:20] if name.strip())
            if joined:
                test_commands.append(f"cd {rel_build} && ctest -R '{joined}' --output-on-failure")
            bench_names = [name.strip() for name in test_names if re.search(r"bench|benchmark|perf|speed", name, re.I)]
            if bench_names:
                joined_bench = "|".join(re.escape(name) for name in bench_names[:20])
                bench_commands.append(f"cd {rel_build} && ctest -R '{joined_bench}' --output-on-failure")
        else:
            test_commands.append(f"cd {rel_build} && ctest --output-on-failure")
    return test_tools, test_commands, bench_commands


def dedupe(values: list[str], limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def normalize_permission_rule(rule: str) -> str | None:
    rule = rule.strip()
    if not rule:
        return None
    malformed_bash = re.fullmatch(r"Bash\s+([^():]+):\*\)", rule)
    if malformed_bash:
        return f"Bash({malformed_bash.group(1).strip()}:*)"
    return rule


def install_batch_claude_settings(workdir: Path) -> Path:
    settings_dir = workdir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"
    data: dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}

    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    existing_allow = permissions.get("allow")
    allow: list[str] = []
    if isinstance(existing_allow, list):
        for item in existing_allow:
            normalized = normalize_permission_rule(str(item))
            if normalized:
                allow.append(normalized)
    allow.extend(BATCH_ALLOWED_PERMISSION_RULES)

    seen: set[str] = set()
    permissions["allow"] = [rule for rule in allow if not (rule in seen or seen.add(rule))]
    permissions["defaultMode"] = "bypassPermissions"
    data["permissions"] = permissions
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return settings_path


def discover_test_commands(project: Path, files: list[Path]) -> tuple[list[str], list[str], list[str]]:
    test_tools: list[str] = []
    test_commands: list[str] = []
    bench_commands: list[str] = []
    names = {relative(path, project) for path in files}

    for make_name in ("Makefile", "makefile", "GNUmakefile"):
        makefile = project / make_name
        if not makefile.exists():
            continue
        targets = parse_make_targets(makefile)
        if targets:
            test_tools.append(f"make targets: {', '.join(sorted(list(targets))[:20])}")
        for target_name in ("test", "check", "unit", "tests"):
            if target_name in targets:
                test_commands.append(f"make {target_name}")
        for target_name in ("bench", "benchmark", "perf", "speed"):
            if target_name in targets:
                bench_commands.append(f"make {target_name}")

    if "CMakeLists.txt" in names:
        test_tools.append("CMake project")
    ctest_tools, ctest_tests, ctest_benches = discover_ctest_candidates(project, files)
    test_tools.extend(ctest_tools)
    test_commands.extend(ctest_tests)
    bench_commands.extend(ctest_benches)

    if "meson.build" in names:
        test_tools.append("Meson project")
        test_commands.append("meson test -C build")
    if "pyproject.toml" in names or "pytest.ini" in names:
        test_tools.append("pytest")
        test_commands.append("pytest")
    if "Cargo.toml" in names:
        test_tools.append("cargo")
        test_commands.append("cargo test")
        bench_commands.append("cargo bench")
    if "go.mod" in names:
        test_tools.append("go test")
        test_commands.append("go test ./...")

    for path in files:
        rel = relative(path, project)
        lower = rel.lower()
        if re.search(r"(^|/)(test_|.*_test|.*test.*\.sh)$", lower):
            test_tools.append(rel)
            if os.access(path, os.X_OK):
                test_commands.append(f"./{rel}")
        if re.search(r"(bench|benchmark|perf|speed)", lower):
            test_tools.append(rel)
            if os.access(path, os.X_OK):
                bench_commands.append(f"./{rel}")

    if not test_commands and any(path.parts and path.parts[0] in {"tests", "test"} for path in (p.relative_to(project) for p in files)):
        test_commands.append("make test")
    if not bench_commands and any("bench" in relative(path, project).lower() for path in files):
        bench_commands.append("make bench")

    return dedupe(test_tools, 20), dedupe(test_commands, 12), dedupe(bench_commands, 12)


def discover_hotspot_candidates(project: Path, files: list[Path]) -> list[dict[str, Any]]:
    all_source_files = [path for path in files if path.suffix in SOURCE_SUFFIXES and safe_file_size(path) < 400_000]
    primary_source_files = [
        path
        for path in all_source_files
        if not re.search(r"(^|/)(test|tests|bench|benchmark|benchmarks)(/|$)", relative(path, project), re.I)
    ]
    source_files = primary_source_files or all_source_files
    ref_text_parts: list[str] = []
    for path in files:
        rel = relative(path, project).lower()
        if re.search(r"(test|bench|benchmark|perf)", rel) and safe_file_size(path) < 200_000:
            ref_text_parts.append(read_limited(path, 80_000).lower())
    ref_text = "\n".join(ref_text_parts)

    candidates: list[dict[str, Any]] = []
    for path in source_files[:800]:
        rel = relative(path, project)
        text = read_limited(path)
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            match = match_function_definition(line)
            if not match:
                continue
            name = match.group(1)
            if name in FUNC_SKIP_NAMES:
                continue
            window = "\n".join(lines[idx : idx + 100])
            lower_name = name.lower()
            priority = 0
            reasons: list[str] = []
            loop_count = len(re.findall(r"\b(for|while)\s*\(", window))
            if loop_count:
                priority += min(loop_count * 2, 6)
                reasons.append(f"{loop_count} loop(s) near definition")
            if PERF_NAME_RE.search(name) or PERF_NAME_RE.search(rel):
                priority += 4
                reasons.append("performance-looking name/path")
            ref_count = ref_text.count(lower_name)
            if ref_count:
                priority += min(ref_count, 4)
                reasons.append(f"referenced by tests/benchmarks {ref_count} time(s)")
            if re.search(r"\b(vld1|vst1|vadd|sv|neon|sve|simd|asm)\b", window, re.I):
                priority += 2
                reasons.append("SIMD/assembly related code nearby")
            if re.search(r"\bsrc|source|kernel|kernels|lib\b", rel, re.I):
                priority += 1
                reasons.append("source/kernel path")
            if priority <= 0:
                continue
            candidates.append(
                {
                    "function_name": name,
                    "code_path": rel,
                    "line": idx + 1,
                    "priority": priority,
                    "measured": False,
                    "reason": "; ".join(reasons),
                }
            )
    candidates.sort(key=lambda item: (-int(item["priority"]), str(item["code_path"]), int(item["line"])))
    return candidates[:20]


def discover_project(source_project: Path, target: dict[str, Any]) -> dict[str, Any]:
    source_project = source_project.expanduser().resolve()
    files = list_project_files(source_project)
    rel_files = [relative(path, source_project) for path in files]
    build_files = [
        rel
        for rel in rel_files
        if Path(rel).name
        in {
            "CMakeLists.txt",
            "Makefile",
            "makefile",
            "GNUmakefile",
            "meson.build",
            "build.ninja",
            "pyproject.toml",
            "Cargo.toml",
            "go.mod",
            "WORKSPACE",
            "MODULE.bazel",
        }
    ]
    source_dirs = sorted(
        {
            rel.split("/", 1)[0]
            for rel in rel_files
            if "/" in rel and rel.split("/", 1)[0] in {"src", "source", "include", "lib", "tests", "test", "bench", "benchmark", "benchmarks", "build"}
        }
    )
    test_tools, test_commands, bench_commands = discover_test_commands(source_project, files)
    hotspots = discover_hotspot_candidates(source_project, files)
    profiling_artifacts = [
        rel
        for rel in rel_files
        if re.search(r"(perf\.data|gmon\.out|flame|profile|prof|benchmark.*\.log|bench.*\.(txt|log|json))", rel, re.I)
    ][:20]

    explicit_function = bool(target.get("code_path") and target.get("function_name"))
    # Default sparse batch targets to testcase mode so /kpbot-code-optimizer's
    # DecomposeTasks profiling selects the real hot functions. Static hotspot
    # discovery remains a hint only; it must not narrow the worker into function
    # mode unless the manifest explicitly pins a source path and function.
    recommended_mode = "function" if explicit_function else "testcase"
    recommended_code_path = str(target.get("code_path") or "")
    recommended_function_name = str(target.get("function_name") or "")

    correctness = str(target.get("test_method") or "")
    benchmark = str(target.get("benchmark_command") or "")
    if not correctness:
        if test_commands and bench_commands:
            correctness = f"{test_commands[0]} && {bench_commands[0]}"
        elif test_commands:
            correctness = test_commands[0]
        elif bench_commands:
            correctness = bench_commands[0]
    if not benchmark and bench_commands:
        benchmark = bench_commands[0]

    confidence = "high: explicit function and test command supplied"
    if not explicit_function:
        if test_commands or bench_commands:
            confidence = "medium: test/benchmark entry points discovered; hotspots are static candidates for /kpbot-code-optimizer"
        else:
            confidence = "low: sparse project signals; worker Claude must inspect and decide inside /kpbot-code-optimizer"

    manifest_mode = str(target.get("mode") or "").strip()
    if manifest_mode == "function" and explicit_function:
        output_mode = "function"
    elif manifest_mode == "testcase":
        output_mode = "testcase"
    else:
        output_mode = recommended_mode

    return {
        "project_root": str(source_project),
        "file_count_scanned": len(files),
        "source_file_count": sum(1 for path in files if path.suffix in SOURCE_SUFFIXES),
        "source_dirs": source_dirs,
        "build_files": build_files[:30],
        "test_tools": test_tools,
        "test_command_candidates": test_commands,
        "benchmark_command_candidates": bench_commands,
        "hotspot_candidates": hotspots,
        "profiling_artifacts": profiling_artifacts,
        "recommended_mode": output_mode,
        "recommended_code_path": recommended_code_path,
        "recommended_function_name": recommended_function_name,
        "recommended_test_method": correctness,
        "recommended_benchmark_command": benchmark,
        "optimization_goal": str(target.get("optimization_goal") or "ARM64/NEON throughput"),
        "confidence": confidence,
    }


def write_discovery_artifacts(discovery: dict[str, Any], target_out: Path) -> None:
    (target_out / "discovery.json").write_text(
        json.dumps(discovery, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Answer Bank Discovery",
        "",
        f"- Project root: `{discovery.get('project_root')}`",
        f"- Files scanned: `{discovery.get('file_count_scanned')}`",
        f"- Recommended mode: `{discovery.get('recommended_mode')}`",
        f"- Recommended code path: `{discovery.get('recommended_code_path') or ''}`",
        f"- Recommended function: `{discovery.get('recommended_function_name') or ''}`",
        f"- Recommended test method: `{discovery.get('recommended_test_method') or ''}`",
        f"- Recommended benchmark: `{discovery.get('recommended_benchmark_command') or ''}`",
        f"- Confidence: `{discovery.get('confidence')}`",
        "",
        "## Build And Test Signals",
        "",
        f"- Source dirs: `{', '.join(discovery.get('source_dirs') or [])}`",
        f"- Build files: `{', '.join(discovery.get('build_files') or [])}`",
        f"- Test tools: `{', '.join(discovery.get('test_tools') or [])}`",
        f"- Test command candidates: `{', '.join(discovery.get('test_command_candidates') or [])}`",
        f"- Benchmark command candidates: `{', '.join(discovery.get('benchmark_command_candidates') or [])}`",
        "",
        "## Hotspot Candidates",
        "",
    ]
    hotspots = discovery.get("hotspot_candidates") or []
    if hotspots:
        lines.extend(["| Rank | Function | Path | Priority | Evidence |", "|---:|---|---|---:|---|"])
        for idx, item in enumerate(hotspots[:12], start=1):
            reason = str(item.get("reason") or "").replace("|", "\\|")
            lines.append(
                f"| {idx} | `{item.get('function_name')}` | `{item.get('code_path')}:{item.get('line')}` | "
                f"{item.get('priority')} | {reason} |"
            )
    else:
        lines.append("No hotspot candidates found by static discovery.")
    lines.append("")
    (target_out / "discovery.md").write_text("\n".join(lines), encoding="utf-8")


def render_candidates(candidates: list[str]) -> str:
    return "; ".join(candidates[:8]) if candidates else "none discovered; worker Claude should inspect project"


def render_hotspots_for_answer_bank(discovery: dict[str, Any], workdir: Path) -> str:
    hotspots = discovery.get("hotspot_candidates") or []
    if not hotspots:
        return "none discovered statically; worker Claude should profile/decompose inside /kpbot-code-optimizer"
    rendered: list[str] = []
    for item in hotspots[:8]:
        path = item.get("code_path") or ""
        work_path = (workdir / str(path)).resolve() if path else workdir
        rendered.append(
            f"{work_path}:{item.get('line')}:{item.get('function_name')} "
            f"(static_hint_priority={item.get('priority')}, measured=false, must_confirm_with_runtime_profiling=true, evidence={item.get('reason')})"
        )
    return "; ".join(rendered)


def build_answer_bank(target: dict[str, Any], workdir: Path, discovery: dict[str, Any]) -> str:
    raw_mode = target.get("mode")
    smoke_mode = raw_mode == "smoke"
    explicit_function = bool(
        (target.get("code_path") or discovery.get("recommended_code_path"))
        and (target.get("function_name") or discovery.get("recommended_function_name"))
    )
    requested_mode = target.get("optimization_mode") or (None if smoke_mode else raw_mode)
    if requested_mode == "function" and not explicit_function:
        requested_mode = None
    mode = requested_mode or discovery.get("recommended_mode") or ("function" if explicit_function else "testcase")
    code_path = target.get("code_path") or discovery.get("recommended_code_path")
    code_abs = str((workdir / code_path).resolve()) if code_path and not str(code_path).startswith("/") else str(code_path or "")
    function_name = target.get("function_name") or discovery.get("recommended_function_name") or ""
    test_method = target.get("test_method") or discovery.get("recommended_test_method") or "worker Claude should select the best discovered test/benchmark command"
    benchmark = target.get("benchmark_command") or discovery.get("recommended_benchmark_command") or ""
    goal = target.get("optimization_goal") or discovery.get("optimization_goal") or "ARM64/NEON throughput"
    fast_path = smoke_mode and bool(target.get("acceptance_fast_path"))
    preferred_strategy = target.get("preferred_strategy") or "explicit ARM NEON intrinsics for the requested function"

    if mode == "function" and code_path and function_name:
        choice = (
            "选择：函数优化\n"
            f"项目路径：{workdir}\n"
            f"源码路径：{code_abs}\n"
            f"函数名：{function_name}\n"
            f"测试命令：{test_method}\n"
        )
    else:
        choice = f"选择：用例优化\n项目路径：{workdir}\n测试命令：{test_method}\n"

    fast_path_text = ""
    if fast_path:
        fast_path_text = (
            "- batch_mode: smoke\n"
            "- acceptance_fast_path: true\n"
            "- max_optimization_rounds: 1\n"
            "- max_optimization_points_to_apply: 1\n"
            "- stop_condition: after one optimization point is verified as applied, skipped, or rolled back, immediately write the final summary\n"
            "- do_not_explore_secondary_points: true\n"
            f"- preferred_first_strategy: {preferred_strategy}\n"
        )

    return (
        "Answer bank:\n"
        f"- project_root: {workdir}\n"
        f"- structure/build_system: source_dirs={', '.join(discovery.get('source_dirs') or [])}; "
        f"build_files={', '.join(discovery.get('build_files') or [])}\n"
        f"- test_tools: {render_candidates(discovery.get('test_tools') or [])}\n"
        f"- test_command_candidates: {render_candidates(discovery.get('test_command_candidates') or [])}\n"
        f"- benchmark_command_candidates: {render_candidates(discovery.get('benchmark_command_candidates') or [])}\n"
        f"- hotspot_candidates: {render_hotspots_for_answer_bank(discovery, workdir)}\n"
        f"- profiling_artifacts: {render_candidates(discovery.get('profiling_artifacts') or [])}\n"
        f"- confidence: {discovery.get('confidence')}\n"
        f"- recommended_mode: {mode}\n"
        f"- recommended_code_path: {code_abs}\n"
        f"- recommended_function_name: {function_name}\n"
        f"- recommended_test_method: {test_method}\n"
        f"- recommended_benchmark_command: {benchmark or test_method}\n"
        f"- optimization_goal: {goal}\n"
        "- target_arch: ARM64/NEON host if available; otherwise continue with portable analysis and reporting\n"
        "- commit_policy: original repository must not be modified; this is an isolated temporary copy, internal commits are allowed\n"
        "- round_policy: formal batch mode requires the full /kpbot-code-optimizer flow; run up to 5 rounds unless no confirmed optimization point remains after verification\n"
        "- optimization_limits: per round analyze up to 3 hotspot functions and up to 3 optimization points per function when available\n"
        "- baseline_policy: if baseline build or tests fail, stop as baseline_blocked and do not apply code changes\n"
        "- verification_policy: applied code must pass build, functional tests, adversarial review, and performance verification before being reported as successful\n"
        "- completion_contract: when the target is finished, write optimization_reports/run_*/stages/*.json for completed stages, optimization_reports/run_*/points/*.json for optimization-point decisions/apply/verify evidence, and optimization_reports/run_*/batch_result.json with pipeline_status, quality_status, applied_count, verified_count, clean_patch_files, blocked_reason, and performance_summary; then print SESSION_COMPLETE, BATCH_END, and SESSION_END before exiting\n"
        "- discovery_policy: outer batch driver used read-only static/listing probes only; worker Claude must confirm real hotspots, tests, benchmarks, and edits inside /kpbot-code-optimizer\n"
        "- static_hotspot_policy: static hotspot candidates are unmeasured hints only; ignore build tools, tests, conditional-only paths, and already-optimized SIMD/assembly labels unless runtime profiling confirms impact\n"
        f"{fast_path_text}\n"
        f"{choice}"
        f"benchmark_command：{benchmark or test_method}\n"
        f"优化目标：{goal}\n"
        "运行模式：自动模式 (auto)\n"
        "请从 /kpbot-code-optimizer 唯一入口完成完整流程，所有阶段自动同意并持续到结束报告。\n"
    )


def install_answer_bank_in_workdir(workdir: Path, answer_bank: str) -> None:
    bank_path = workdir / ".batch_optimize_answer_bank.md"
    bank_path.write_text(answer_bank, encoding="utf-8")
    settings_path = install_batch_claude_settings(workdir)

    claude_path = workdir / "CLAUDE.md"
    batch_instructions = (
        "\n\n# Batch Optimize Pipeline Context\n\n"
        "When `/kpbot-code-optimizer` is invoked in this temporary worktree, first read "
        "`.batch_optimize_answer_bank.md`. Use its project_root, code_path, "
        "function_name, test_method, benchmark_command, and optimization_goal values "
        "to answer GatherContext. Choose auto mode when offered. Continue ordinary "
        "stage confirmations and optimization rounds through the full formal pipeline: "
        "PrepareProject, DecomposeTasks, AnalyzeTestcase, AnalyzeHotspot, DecideOptimization, "
        "ApplyOptimization, AdversarialReview, and VerifyOptimization when an optimization "
        "point is confirmed. Do not stop early only because a static hotspot appears optimized; "
        "confirm with runtime profiling and verification evidence. The original repository is "
        "outside this temporary copy and must not be modified. Each completed stage must write "
        "`optimization_reports/run_*/stages/<stage>.json`; each optimization-point decision/apply/"
        "verify result must write `optimization_reports/run_*/points/<round>_<function>_<opt>.json`; "
        "the target must finish with `optimization_reports/run_*/batch_result.json` containing "
        "`pipeline_status`, `quality_status`, `applied_count`, `verified_count`, `clean_patch_files`, "
        "`blocked_reason`, and `performance_summary`. Then print `SESSION_COMPLETE`, `BATCH_END`, and "
        "`SESSION_END` before exiting.\n"
    )
    if claude_path.exists():
        claude_path.write_text(claude_path.read_text(encoding="utf-8") + batch_instructions, encoding="utf-8")
    else:
        claude_path.write_text(batch_instructions.lstrip(), encoding="utf-8")

    add_paths = [".batch_optimize_answer_bank.md", "CLAUDE.md", relative(settings_path, workdir)]
    skills_path = workdir / ".claude" / "skills"
    if has_optimize_pipeline_skill(skills_path):
        add_paths.append(relative(skills_path, workdir))
    run(["git", "add", "-A", "-f", *add_paths], workdir)
    run(["git", "commit", "--amend", "--no-edit"], workdir)
    run(["git", "tag", "-f", "batch_baseline"], workdir)


def write_target_state(target_out: Path, updates: dict[str, Any]) -> dict[str, Any]:
    path = target_out / "target_state.json"
    state = read_json_file(path)
    state.update(updates)
    state["updated_at"] = utc_now()
    write_json_file(path, state)
    return state


def artifact_listing_for_resume(workdir: Path, target_out: Path) -> list[str]:
    artifacts: list[str] = []
    reports = workdir / "optimization_reports"
    if reports.exists():
        for path in sorted(reports.rglob("*")):
            if path.is_file():
                try:
                    artifacts.append(str(path.relative_to(workdir)))
                except ValueError:
                    artifacts.append(str(path))
                if len(artifacts) >= 12:
                    break
    for name in ["progress.json", "completion_gate.json", "final.patch", "target_result.json"]:
        if (target_out / name).exists():
            artifacts.append(f"target_artifact:{name}")
    return artifacts


def truncate_to_token_budget(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return text
    tokens, _, _ = estimate_text_tokens(text)
    if tokens <= max_tokens:
        return text
    chars = max(1000, int(len(text) * max_tokens / max(tokens, 1)))
    return text[:chars].rstrip() + "\n[resume prompt truncated to token budget]\n"


def build_resume_prompt(
    target_id: str,
    workdir: Path,
    target_out: Path,
    runner_result: dict[str, Any] | None,
    max_tokens: int,
    transcript_tail_lines: int,
) -> str:
    progress = read_json_file(target_out / "progress.json")
    gate = read_json_file(target_out / "completion_gate.json")
    artifacts = artifact_listing_for_resume(workdir, target_out)
    transcript_tail = ""
    transcript = target_out / "claude_log" / "transcript_clean.md"
    if transcript.exists() and transcript_tail_lines > 0:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        transcript_tail = "\n".join(lines[-transcript_tail_lines:])
    missing: list[str] = []
    if gate:
        for key in [
            "pipeline_reached_final_report",
            "required_stages_seen",
            "functional_verified",
            "performance_measured",
            "artifact_consistent",
        ]:
            if gate.get(key) is False:
                missing.append(key)
    reason = (runner_result or {}).get("termination_reason") or (runner_result or {}).get("error") or "previous worker did not complete"
    prompt = (
        "\n\n# Batch Managed Resume Prompt\n\n"
        f"Target: {target_id}\n"
        f"Workdir: {workdir}\n"
        f"Previous termination: {reason}\n"
        f"Visible phase: {progress.get('phase') or 'unknown'}\n"
        f"Task counts: {json.dumps(progress.get('task_counts') or {}, ensure_ascii=False)}\n"
        f"Stage snapshot: {json.dumps(progress.get('stages') or [], ensure_ascii=False)}\n"
        f"Known artifacts: {json.dumps(artifacts, ensure_ascii=False)}\n"
        f"Missing completion gate fields: {', '.join(missing) if missing else 'not yet graded'}\n\n"
        "继续上一次 /kpbot-code-optimizer 工作流。只从已有仓库状态、optimization_reports、checkpoint 和当前 patch 恢复，"
        "不要重新复制项目，不要修改原始仓库。必须补齐缺失阶段、功能验证、性能验证和最终报告；"
        "如果确认没有有效优化，写明完整分析证据并产出 complete_no_optimization。\n"
    )
    if transcript_tail:
        prompt += "\nRecent transcript tail for recovery only:\n```text\n" + transcript_tail + "\n```\n"
    return truncate_to_token_budget(prompt, max_tokens)


def attempt_record(attempt: int, started_at: str, duration_seconds: int, runner_result: dict[str, Any]) -> dict[str, Any]:
    usage = runner_result.get("usage") if isinstance(runner_result.get("usage"), dict) else {}
    timing = runner_result.get("timing") if isinstance(runner_result.get("timing"), dict) else {}
    return {
        "attempt_index": attempt,
        "started_at": started_at,
        "ended_at": runner_result.get("ended_at") or utc_now(),
        "duration_seconds": int(timing.get("duration_seconds") or duration_seconds),
        "runner_status": runner_result.get("status"),
        "exit_code": runner_result.get("exit_code"),
        "final_marker_seen": runner_result.get("final_marker_seen"),
        "termination_reason": runner_result.get("termination_reason") or runner_result.get("error") or runner_result.get("status"),
        "context_window_peak_estimate": usage.get("context_window_peak_estimate"),
        "usage": usage,
        "timing": timing,
    }


def aggregate_usage(target_out: Path, answer_bank_file: Path, attempts: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    prompt_text = answer_bank_file.read_text(encoding="utf-8", errors="replace") if answer_bank_file.exists() else ""
    driver_prompt_tokens, method, _ = estimate_text_tokens(prompt_text)
    transcript_tokens = int((result.get("transcript_token_stats") or {}).get("token_count") or 0)
    input_tokens = 0
    output_tokens = 0
    context_peak = driver_prompt_tokens
    usage_attempts: list[dict[str, Any]] = []
    exact = False
    for attempt in attempts:
        usage = attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {}
        input_tokens += int(usage.get("worker_input_tokens_estimate") or 0)
        output_tokens += int(usage.get("worker_output_tokens_estimate") or 0)
        context_peak = max(context_peak, int(usage.get("context_window_peak_estimate") or 0))
        exact = exact or bool(usage.get("is_exact"))
        usage_attempts.append(
            {
                "attempt_index": attempt.get("attempt_index"),
                "input_tokens_estimate": usage.get("worker_input_tokens_estimate"),
                "output_tokens_estimate": usage.get("worker_output_tokens_estimate"),
                "transcript_tokens_estimate": usage.get("transcript_tokens_estimate"),
                "context_window_peak_estimate": usage.get("context_window_peak_estimate"),
                "termination_reason": attempt.get("termination_reason"),
            }
        )
        if usage.get("method"):
            method = str(usage.get("method"))
    if not transcript_tokens:
        transcript_tokens = input_tokens + output_tokens
    summary = {
        "worker_input_tokens_estimate": input_tokens,
        "worker_output_tokens_estimate": output_tokens,
        "transcript_tokens_estimate": transcript_tokens,
        "driver_prompt_tokens_estimate": driver_prompt_tokens,
        "context_window_peak_estimate": context_peak,
        "attempts": usage_attempts,
        "method": method,
        "is_exact": exact,
    }
    write_json_file(target_out / "usage.json", summary)
    return summary


def aggregate_timing(target_out: Path, started_monotonic: float, started_at: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    ended_at = utc_now()
    wall_time = int(time.monotonic() - started_monotonic)
    active_worker = sum(int(attempt.get("duration_seconds") or 0) for attempt in attempts)
    idle_total = sum(int(((attempt.get("timing") or {}) if isinstance(attempt.get("timing"), dict) else {}).get("idle_seconds_final") or 0) for attempt in attempts)
    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_time_seconds": wall_time,
        "active_worker_seconds": active_worker,
        "idle_seconds_total": idle_total,
        "driver_overhead_seconds": max(0, wall_time - active_worker),
        "attempt_count": len(attempts),
        "attempts": attempts,
    }
    write_json_file(target_out / "timing.json", summary)
    return summary


def invoke_auto_claude(
    workdir: Path,
    target_out: Path,
    answer_bank_file: Path,
    claude_bin: str | None,
    timeout_minutes: int,
    idle_timeout_minutes: int,
    progress_interval_seconds: float,
    attempt_index: int = 0,
    context_soft_limit_tokens: int = DEFAULT_CONTEXT_SOFT_LIMIT_TOKENS,
    context_hard_limit_tokens: int = DEFAULT_CONTEXT_HARD_LIMIT_TOKENS,
    transcript_tail_lines: int = DEFAULT_TRANSCRIPT_TAIL_LINES,
    progress_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    log_dir = target_out / "claude_log"
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "auto_claude_logged.py"),
        "--project-path",
        str(workdir),
        "--log-dir",
        str(log_dir),
        "--answer-bank-file",
        str(answer_bank_file),
        "--timeout-seconds",
        str(timeout_minutes * 60),
        "--idle-timeout-seconds",
        str(idle_timeout_minutes * 60),
        "--progress-interval-seconds",
        str(progress_interval_seconds),
        "--attempt-index",
        str(attempt_index),
        "--transcript-tail-lines",
        str(transcript_tail_lines),
    ]
    if context_soft_limit_tokens > 0:
        cmd += ["--context-soft-limit-tokens", str(context_soft_limit_tokens)]
    if context_hard_limit_tokens > 0:
        cmd += ["--context-hard-limit-tokens", str(context_hard_limit_tokens)]
    if claude_bin:
        cmd += ["--claude-bin", claude_bin]
    returncode, stdout_tail = run_streamed(
        cmd,
        progress_callback=progress_callback,
        progress_interval_seconds=progress_interval_seconds,
        timeout_seconds=timeout_minutes * 60 + 30,
    )
    result_path = log_dir / "auto_result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {
            "status": "failed",
            "error": f"auto runner did not write result; exit={returncode}; output={stdout_tail[-2000:]}",
            "exit_code": returncode,
            "log_dir": str(log_dir),
        }
    if returncode != 0 and result.get("status") == "running":
        result["status"] = "failed"
        result["error"] = result.get("error") or f"auto runner exited with code {returncode} before final result"
        result["exit_code"] = returncode
    result["runner_stdout"] = stdout_tail[-4000:]
    return result


def run_target(
    target: dict[str, Any],
    out: Path,
    run_cfg: dict[str, Any],
    claude_bin: str | None,
    legacy_driver: bool = False,
    progress_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    target_id = target_manifest_id(target)
    target_out = out / "targets" / target_id
    workdir = out / "workdirs" / target_id
    target_out.mkdir(parents=True, exist_ok=True)
    target_started_monotonic = time.monotonic()
    target_started_at = utc_now()
    attempts_path = target_out / "attempts.jsonl"
    if attempts_path.exists():
        attempts_path.unlink()

    source_project = Path(str(target["project_path"])).expanduser().resolve()
    source_status_before = project_status(source_project)
    write_target_state(
        target_out,
        {
            "driver": "legacy" if legacy_driver else MANAGED_DRIVER_VERSION,
            "target_id": target_id,
            "run_status": "initializing",
            "pipeline_status": "unknown",
            "quality_status": "unknown",
            "source_project": str(source_project),
            "workdir": str(workdir),
            "target_out": str(target_out),
            "started_at": target_started_at,
        },
    )
    discovery = discover_project(source_project, target)
    write_discovery_artifacts(discovery, target_out)
    copy_project(source_project, workdir)
    ensure_workdir_pipeline_skills(workdir, source_project)

    answer_bank = build_answer_bank(target, workdir, discovery)
    answer_bank_file = target_out / "answer_bank.txt"
    answer_bank_file.write_text(answer_bank, encoding="utf-8")
    install_answer_bank_in_workdir(workdir, answer_bank)

    timeout_minutes = int(target.get("timeout_minutes") or run_cfg.get("timeout_minutes") or DEFAULT_TIMEOUT_MINUTES)
    idle_timeout_minutes = int(
        target.get("idle_timeout_minutes") or run_cfg.get("idle_timeout_minutes") or DEFAULT_IDLE_TIMEOUT_MINUTES
    )
    progress_interval_seconds = float(
        target.get("progress_interval_seconds")
        or run_cfg.get("progress_interval_seconds")
        or DEFAULT_PROGRESS_INTERVAL_SECONDS
    )
    resume_attempts = int(target.get("resume_attempts") or run_cfg.get("resume_attempts") or DEFAULT_RESUME_ATTEMPTS)
    context_soft_limit_tokens = config_int(
        target, run_cfg, "context_soft_limit_tokens", DEFAULT_CONTEXT_SOFT_LIMIT_TOKENS
    )
    context_hard_limit_tokens = config_int(
        target, run_cfg, "context_hard_limit_tokens", DEFAULT_CONTEXT_HARD_LIMIT_TOKENS
    )
    resume_prompt_max_tokens = config_int(target, run_cfg, "resume_prompt_max_tokens", DEFAULT_RESUME_PROMPT_MAX_TOKENS)
    transcript_tail_lines = int(
        target.get("transcript_tail_lines")
        or run_cfg.get("transcript_tail_lines")
        or DEFAULT_TRANSCRIPT_TAIL_LINES
    )
    if legacy_driver:
        context_soft_limit_tokens = 0
        context_hard_limit_tokens = 0

    stage_executor = StageExecutor(
        invoke_worker=invoke_auto_claude,
        attempt_record=attempt_record,
        build_resume_prompt=build_resume_prompt,
        write_target_state=write_target_state,
        append_jsonl=append_jsonl,
        utc_now=utc_now,
        progress_callback=progress_callback,
    )
    runner_result, attempts = stage_executor.run(
        StageExecutorConfig(
            target_id=target_id,
            workdir=workdir,
            target_out=target_out,
            answer_bank_file=answer_bank_file,
            claude_bin=claude_bin,
            timeout_minutes=timeout_minutes,
            idle_timeout_minutes=idle_timeout_minutes,
            progress_interval_seconds=progress_interval_seconds,
            resume_attempts=resume_attempts,
            context_soft_limit_tokens=context_soft_limit_tokens,
            context_hard_limit_tokens=context_hard_limit_tokens,
            resume_prompt_max_tokens=resume_prompt_max_tokens,
            transcript_tail_lines=transcript_tail_lines,
            legacy_driver=legacy_driver,
        ),
        attempts_path,
    )

    write_target_state(target_out, {"run_status": "collecting", "attempt_count": len(attempts)})
    result = collect_target_result(
        target_id=target_id,
        workdir=workdir,
        target_out=target_out,
        log_dir=target_out / "claude_log",
        runner_result=runner_result,
        source_project=source_project,
    )
    source_status_after = project_status(source_project)
    result["source_status_before"] = source_status_before
    result["source_status_after"] = source_status_after
    result["source_unchanged"] = source_status_before == source_status_after
    result["discovery"] = str(target_out / "discovery.json")
    result["discovery_report"] = str(target_out / "discovery.md")
    usage = aggregate_usage(target_out, answer_bank_file, attempts, result)
    timing = aggregate_timing(target_out, target_started_monotonic, target_started_at, attempts)
    result["usage"] = usage
    result["usage_path"] = str(target_out / "usage.json")
    result["timing"] = timing
    result["timing_path"] = str(target_out / "timing.json")
    result["attempts_path"] = str(attempts_path)
    result["driver"] = "legacy" if legacy_driver else MANAGED_DRIVER_VERSION
    result["run_status"] = "completed" if result.get("completion_status") == "completed" else "failed"
    result["pipeline_status"] = "completed" if result.get("reached_final_summary") else "incomplete"
    if not result["source_unchanged"] and result["status"] in SUCCESS_STATUSES:
        result["status"] = "artifact_error"
        result["quality_status"] = "artifact_error"
        result["blocker"] = "source repository status changed during batch run"
        result["run_status"] = "failed"
    (target_out / "target_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_target_state(
        target_out,
        {
            "run_status": result.get("run_status"),
            "pipeline_status": result.get("pipeline_status"),
            "quality_status": result.get("quality_status") or result.get("status"),
            "completion_status": result.get("completion_status"),
            "blocker": result.get("blocker"),
            "attempt_count": len(attempts),
            "usage": {
                "transcript_tokens_estimate": usage.get("transcript_tokens_estimate"),
                "context_window_peak_estimate": usage.get("context_window_peak_estimate"),
            },
            "timing": {
                "wall_time_seconds": timing.get("wall_time_seconds"),
                "active_worker_seconds": timing.get("active_worker_seconds"),
            },
        },
    )
    with (target_out / "target_report.md").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## Answer Bank Discovery\n\n"
            f"- Discovery JSON: `{target_out / 'discovery.json'}`\n"
            f"- Discovery report: `{target_out / 'discovery.md'}`\n"
            f"- Recommended mode: `{discovery.get('recommended_mode')}`\n"
            f"- Recommended function: `{discovery.get('recommended_code_path') or ''}:{discovery.get('recommended_function_name') or ''}`\n"
            f"- Recommended test method: `{discovery.get('recommended_test_method') or ''}`\n"
            f"- Recommended benchmark: `{discovery.get('recommended_benchmark_command') or ''}`\n"
            f"- Confidence: `{discovery.get('confidence')}`\n"
        )
    print(
        f"[batch-target] collected target={target_id} status={result.get('status')} "
        f"report={target_out / 'target_report.md'}",
        flush=True,
    )
    if progress_callback:
        progress_callback()
    return result


def result_token_count(item: dict[str, Any]) -> int:
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    if usage.get("transcript_tokens_estimate") is not None:
        return int(usage.get("transcript_tokens_estimate") or 0)
    return int((item.get("transcript_token_stats") or {}).get("token_count") or 0)


def result_wall_time(item: dict[str, Any]) -> int:
    timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
    return int(timing.get("wall_time_seconds") or 0)


def result_attempt_count(item: dict[str, Any]) -> int:
    timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
    if timing.get("attempt_count") is not None:
        return int(timing.get("attempt_count") or 0)
    runner = item.get("runner") if isinstance(item.get("runner"), dict) else {}
    return int(runner.get("attempt") or 0) + 1 if runner else 0


def target_effect_summary(item: dict[str, Any]) -> str:
    target_out = Path(str(item.get("target_out") or "."))
    text = ""
    final_summary = target_out / "final_summary.md"
    if final_summary.exists():
        text = final_summary.read_text(encoding="utf-8", errors="replace")
    blocker = str(item.get("blocker") or "")
    combined = "\n".join([text, blocker])
    patterns = [
        r"(\+\s*\d+(?:\.\d+)?\s*%)",
        r"(\d+(?:\.\d+)?\s*x\s*(?:speedup|加速|提升)?)",
        r"(cycles[^\n]{0,60}(?:-\s*)?\d+(?:\.\d+)?\s*%)",
        r"(IPC[^\n]{0,60}\+?\s*\d+(?:\.\d+)?\s*%)",
        r"(No optimization possible|No code changes required|complete_no_optimization|无有效优化|未产生有效优化)",
    ]
    for pattern in patterns:
        match = re.search(pattern, combined, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    if item.get("status") == "applied_verified":
        return "verified improvement measured"
    if item.get("status") in {"analysis_only", "complete_no_optimization"}:
        return "no effective optimization"
    return "-"


def write_summary(out: Path, results: list[dict[str, Any]]) -> None:
    ability_evidence = write_batch_evidence(out, results)
    target_ability = {
        str(item.get("target_id")): item
        for item in ability_evidence.get("targets", [])
        if isinstance(item, dict)
    }
    successful = sum(1 for item in results if item.get("status") in SUCCESS_STATUSES)
    failed = len(results) - successful
    status_counts: dict[str, int] = {}
    completion_counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        completion = str(item.get("completion_status") or ("completed" if item.get("reached_final_summary") else "unknown"))
        completion_counts[completion] = completion_counts.get(completion, 0) + 1
    total_transcript_tokens = sum(result_token_count(item) for item in results)
    total_wall_time = sum(result_wall_time(item) for item in results)
    summary = {
        "status": "completed" if failed == 0 else "failed",
        "successful": successful,
        "failed": failed,
        "status_counts": status_counts,
        "completion_counts": completion_counts,
        "transcript_tokens_total": total_transcript_tokens,
        "wall_time_seconds_total": total_wall_time,
        "ability_evidence": {
            "path": str(out / "ability_evidence.json"),
            "status_counts": ability_evidence.get("status_counts") or {},
            "risk_flag_counts": ability_evidence.get("risk_flag_counts") or {},
            "evidence_counts": ability_evidence.get("evidence_counts") or {},
        },
        "targets": results,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Batch Drive Optimize Pipeline Summary",
        "",
        f"- Status: `{summary['status']}`",
        f"- Successful: `{successful}`",
        f"- Failed: `{failed}`",
        f"- Status counts: `{json.dumps(status_counts, ensure_ascii=False, sort_keys=True)}`",
        f"- Completion counts: `{json.dumps(completion_counts, ensure_ascii=False, sort_keys=True)}`",
        f"- Transcript tokens total: `{total_transcript_tokens:,}`",
        f"- Wall time total: `{format_elapsed(total_wall_time)}`",
        f"- Ability evidence: `{out / 'ability_evidence.md'}`",
        f"- Monitor snapshot: `{out / 'status_snapshot.json'}`",
        "",
        "| Target | Run | Pipeline | Quality | Risk flags | Tokens | Time | Attempts | Effect | Report | Blocker |",
        "|---|---|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for item in results:
        report = Path(item["target_out"]) / "target_report.md"
        blocker = (item.get("blocker") or "").replace("|", "\\|")
        token_count = result_token_count(item)
        wall_time = result_wall_time(item)
        attempts = result_attempt_count(item)
        effect = target_effect_summary(item).replace("|", "\\|")
        ability = target_ability.get(str(item.get("target_id"))) or {}
        risk_flags = ", ".join(ability.get("risk_flags") or []) or "none"
        lines.append(
            f"| `{item['target_id']}` | `{item.get('run_status') or 'unknown'}` | "
            f"`{item.get('pipeline_status') or item.get('completion_status') or 'unknown'}` | "
            f"`{item.get('quality_status') or item.get('status')}` | `{risk_flags}` | `{token_count:,}` | "
            f"`{format_elapsed(wall_time) if wall_time else '-'}` | `{attempts}` | {effect} | `{report}` | {blocker} |"
        )
    lines.append("")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def make_self_test_manifest(out: Path) -> dict[str, Any]:
    source = out / "self_test_source"
    create_project(source, force=True)
    return {
        "run": {
            "max_parallel": 1,
            "timeout_minutes": DEFAULT_TIMEOUT_MINUTES,
            "idle_timeout_minutes": DEFAULT_IDLE_TIMEOUT_MINUTES,
            "resume_attempts": DEFAULT_RESUME_ATTEMPTS,
            "keep_workdirs": "on_failure",
        },
        "targets": [
            {
                "id": "real_claude_vec_add",
                "project_path": str(source),
                "mode": "smoke",
                "optimization_goal": "ARM64/NEON throughput",
                "acceptance_fast_path": True,
                "preferred_strategy": "explicit ARM NEON intrinsics vectorization for vec_add",
            }
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", help="JSON or simple YAML manifest.")
    parser.add_argument(
        "--default-manifest",
        default=str(DEFAULT_MANIFEST),
        help="Manifest used when --manifest is omitted.",
    )
    parser.add_argument(
        "--self-test",
        "--self-test-real-claude",
        dest="self_test_real_claude",
        action="store_true",
        help="Run the real-Claude self-test instead of a manifest batch.",
    )
    parser.add_argument("--out")
    parser.add_argument("--targets", help="Comma or whitespace separated target ids to run.")
    parser.add_argument("--list-targets", action="store_true", help="List manifest target ids and exit.")
    parser.add_argument(
        "--non-interactive",
        "--yes",
        dest="non_interactive",
        action="store_true",
        help="Fail fast instead of asking questions; accepted for slash-command launchers.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Reserved for outer slash-command menus; this script remains non-interactive.",
    )
    parser.add_argument("--claude-bin")
    parser.add_argument("--timeout-minutes", type=int)
    parser.add_argument("--idle-timeout-minutes", type=int)
    parser.add_argument("--legacy-driver", action="store_true", help="Use the previous long-session batch driver behavior.")
    parser.add_argument("--context-soft-limit-tokens", type=int, default=None)
    parser.add_argument("--context-hard-limit-tokens", type=int, default=None)
    parser.add_argument("--resume-prompt-max-tokens", type=int, default=None)
    parser.add_argument("--transcript-tail-lines", type=int, default=None)
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Print a bounded live status snapshot for --out and exit without reading transcript bodies.",
    )
    parser.add_argument(
        "--monitor-format",
        choices=["text", "json"],
        default="text",
        help="Output format for --monitor.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    validate_args(args)

    if args.monitor:
        out = Path(args.out).expanduser().resolve()
        snapshot = monitor_status_snapshot(out)
        if args.monitor_format == "json":
            print(json.dumps(snapshot, ensure_ascii=False, indent=2), flush=True)
        else:
            print(render_monitor_snapshot(snapshot), flush=True)
        return 0

    if args.list_targets:
        print_target_list(load_targets_for_listing(args))
        return 0

    context = load_batch_context(args, make_self_test_manifest=make_self_test_manifest)
    return BatchPipeline(
        context=context,
        legacy_driver=args.legacy_driver,
        managed_driver_version=MANAGED_DRIVER_VERSION,
        run_target=run_target,
        write_summary=write_summary,
        progress_renderer=print_batch_progress,
        now=utc_now,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
