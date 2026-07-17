#!/usr/bin/env python3
"""Run worker Claude through /kpbot-code-optimizer with automatic replies and logs."""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from config import (  # noqa: E402
    DEFAULT_ATTEMPT_INDEX,
    DEFAULT_CONTEXT_HARD_LIMIT_TOKENS,
    DEFAULT_CONTEXT_SOFT_LIMIT_TOKENS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_PROMPT_REPEATS,
    DEFAULT_MIN_REPLY_INTERVAL_SECONDS,
    DEFAULT_NUDGE_AFTER_SECONDS,
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TRANSCRIPT_TAIL_LINES,
)
from log_handler import (  # noqa: E402
    clean_terminal_text,
    safe_text,
    utc_now,
    write_json_atomic,
)
from safe_env import child_env  # noqa: E402
from session_manager import SessionManager  # noqa: E402


FINAL_RE = re.compile(
    r"(鲲鹏性能优化流水线完成|总轮次(数)?|累计(优化|吞吐)?提升|累计优化效果|"
    r"持续进化入口|进化入口|已达最大轮次|final_summary\.md|FINAL_SUMMARY\.md|"
    r"Pipeline\s+Complete|batch_result\.json|complete_no_optimization|"
    r"SESSION_COMPLETE|BATCH_END|SESSION_END|TARGET_COMPLETE|BATCH_SUMMARY|"
    r"pipeline_status.{0,20}(complete|completed)|target_status.{0,20}(complete|completed)|"
    r"所有轮次.{0,20}完成|优化流程.{0,20}(完成|结束)|"
    r"final completed result|closing report.{0,40}(complete|completed)|"
    r"最终(输出|结果|报告).{0,40}(完成|如下))",
    re.IGNORECASE,
)

FINAL_REPORT_NAMES = {"final_summary.md", "final_report.md", "final_summary.md", "final_report.md"}
COMPLETE_STATUSES = {
    "complete",
    "completed",
    "complete_no_optimization",
    "analysis_complete",
    "success",
    "succeeded",
    "ok",
    "done",
}
PROMPT_AREA_CHARS = 1800
RECENT_CONTEXT_CHARS = 5000
TOKENISH_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u3400-\u9fff]", re.UNICODE)
_TOKEN_ENCODER: Any | None = None
_TOKEN_METHOD: str | None = None

SELECTION_UI_RE = re.compile(
    r"(Enter.*(select|elect|confirm)|↑/↓|navigate|avigate|Type something|Chat ab)",
    re.I,
)
USER_INPUT_PROMPT_RE = re.compile(
    r"(请选择|请回复(?:您的)?选择|请输入|请提供|回复您的选择)",
    re.I,
)
AGENT_STATUS_UI_RE = re.compile(
    r"(Enter\s*to\s*view|ctrl\+b\s*to\s*run\s*in\s*background|general-purpose|main\s*↑/↓)",
    re.I,
)
SETTINGS_WARNING_RE = re.compile(
    r"(SettingsWarning|Invalid\s+permission\s+rule|Fix\s+with\s+Claude|Exit\s+and\s+fix\s+manually)",
    re.I,
)
WORKER_BUSY_RE = re.compile(
    r"(Thought\s+for\s+\d+|Thinking\s+for\s+\d+|Bash\(|Running\s+in\s+the\s+background|"
    r"\d+\s+shells?|ctrl\+t\s+to\s+hide\s+task|esc\s+to\s+interrupt|"
    r"架构分析|准备项目环境|热点分析|代码优化|对抗性审核|验证优化)",
    re.I,
)
RECAP_PAUSE_RE = re.compile(
    r"(Churned\s+for\s+\d+\s*(?:m|s|h)(?:\s+\d+\s*s)?|※\s*recap\s*:)",
    re.I,
)


SEMANTIC_NOISE_RE = re.compile(
    r"(almost done thinking|thinking(?:\s+some\s+more)?\s+with\s+\w+\s+effort|"
    r"thought\s+for\s+\d+\s*s?|bypass\s+permissions|shift\+tab|esc\s+to\s+interrupt|"
    r"ctrl\+t|enter\s+to\s+view|main\s*↑/↓|general-purpose|tokens?)",
    re.I,
)
SEMANTIC_SIGNAL_RE = re.compile(
    r"(收集优化目标信息|准备项目环境|优化轮次循环|热点分析|代码优化|对抗性审核|验证|"
    r"SubTask|优化点|完成|失败|错误|成功|写入|编译|测试|基线|"
    r"optimization_reports|CHECKPOINT|FINAL_SUMMARY|batch_result|target_result|"
    r"patch|diff|git|make|cmake|ctest|cargo|benchmark|pytest|error|failed|"
    r"passed|success|warning|build|compile|verify|apply|analyze)",
    re.I,
)
AUTO_REPLY_ECHO_LINE_RE = re.compile(
    r"^\s*(?:继续(?:；如果流水线已经完成.*)?|同意，继续下一步。|/kpbot-code-optimizer)\s*$",
    re.I | re.M,
)


def context_ui_limit_reached(text: str) -> bool:
    cleaned = clean_terminal_text(text)
    if not cleaned:
        return False
    compact = re.sub(r"\s+", "", cleaned).lower()
    return bool(
        re.search(r"(?:9[5-9]|100)%context(?:used|usage)", compact)
        or re.search(r"(?:0|1|2|3|4|5)%contextleft", compact)
    )


def is_meaningful_worker_output(text: str) -> bool:
    cleaned = clean_terminal_text(text)
    if not cleaned:
        return False
    cleaned = AUTO_REPLY_ECHO_LINE_RE.sub(" ", cleaned)
    if not cleaned.strip():
        return False
    if SEMANTIC_SIGNAL_RE.search(cleaned):
        return True
    residual = SEMANTIC_NOISE_RE.sub(" ", cleaned)
    residual = re.sub(r"\b\d+\s*(?:s|m|h|ms|tokens?|ktokens?)\b", " ", residual, flags=re.I)
    residual = re.sub(r"[\d\s*·●✶✢✻✽◯◼◻✔❯⏵↑↓/|:;,.()\\[\\]{}<>\-_=+~`'\"\\u2500-\\u257f]+", " ", residual)
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u3400-\u9fff]{2,}", residual)
    return len(words) >= 6


def estimate_token_count(text: str) -> tuple[int, str]:
    cleaned = clean_terminal_text(text)
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
        return len(_TOKEN_ENCODER.encode(cleaned, disallowed_special=())), str(_TOKEN_METHOD)
    return sum(1 for _ in TOKENISH_RE.finditer(cleaned)), str(_TOKEN_METHOD)


def progress_excerpt(text: str, limit: int = 300) -> str:
    cleaned = clean_terminal_text(text)
    if not cleaned:
        return ""
    compact = " ".join(cleaned.split())
    return compact[-limit:]


def last_match(pattern: str, text: str, flags: int = re.I) -> re.Match[str] | None:
    matches = list(re.finditer(pattern, text, flags))
    return matches[-1] if matches else None


def current_task_counts(text: str) -> dict[str, int] | None:
    match = last_match(
        r"(\d+)\s*tasks\s*\(\s*(\d+)\s*done,\s*(\d+)\s*in\s*progress,\s*(\d+)\s*(?:open|pending)",
        text,
    )
    if not match:
        return None
    total, done, in_progress, pending = (int(match.group(i)) for i in range(1, 5))
    return {"total": total, "done": done, "in_progress": in_progress, "pending": pending}


def status_for_label(text: str, label: str, final_seen: bool = False) -> str | None:
    matching_lines = [line for line in text.splitlines() if re.search(re.escape(label), line, re.I)]
    if not matching_lines:
        return None
    line = matching_lines[-1]
    if re.search(r"✔|completed|done|通过|完成", line, re.I):
        return "Completed"
    if re.search(r"◼|in\s*progress|进行中|当前|running", line, re.I):
        return "In Progress"
    if final_seen:
        return "Completed"
    matches = list(re.finditer(re.escape(label), text, re.I))
    match = matches[-1]
    window = text[max(0, match.start() - 30) : match.end() + 60]
    if re.search(r"✔", window):
        return "Completed"
    if re.search(r"◼", window):
        return "In Progress"
    return "Observed"


def progress_stage_rows(text: str, final_seen: bool) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    stage_labels = [
        "收集优化目标信息",
        "准备项目环境",
        "优化轮次循环",
        "热点分析",
        "代码优化",
        "对抗性审核",
        "验证",
    ]
    for label in stage_labels:
        status = status_for_label(text, label, final_seen=final_seen and label in {"收集优化目标信息", "准备项目环境", "验证"})
        if status:
            rows.append((label, status))

    round_match = last_match(r"第\s*(\d+)\s*轮优化", text)
    if round_match:
        label = f"第 {round_match.group(1)} 轮优化"
        status = status_for_label(text, round_match.group(0), final_seen=final_seen) or (
            "Completed" if final_seen else "In Progress"
        )
        rows.append((label, status))

    subtask_match = last_match(r"SubTask\s*#\d+\s*:\s*([A-Za-z0-9_.$:-]+)", text)
    if subtask_match:
        label = subtask_match.group(0).replace("  ", " ")
        status = status_for_label(text, subtask_match.group(0), final_seen=final_seen) or (
            "Completed" if final_seen else "In Progress"
        )
        rows.append((label, status))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, status in rows:
        if label in seen:
            continue
        seen.add(label)
        deduped.append((label, status))
    return deduped[-8:]


def extract_skip_reason(text: str, keyword: str) -> str:
    idx = text.lower().rfind(keyword.lower())
    if idx < 0:
        return ""
    window = text[idx : idx + 1400]
    patterns = [
        r"Skip Reason:\s*(.+?)(?:\n|$)",
        r"skip_reason[\"']?\s*[:=]\s*[\"']([^\"']+)",
        r"SKIPPED\s*(?:with reason:|[-:])?\s*(.+?)(?:\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, window, re.I)
        if match:
            return " ".join(match.group(1).split())[:260]
    if "NEON/SVE" in window or "SVE" in keyword:
        return "Kunpeng NEON/SVE share vector execution resources; no throughput gain for compute-bound hashing."
    if "always_inline" in window or "round" in keyword.lower():
        return "round_fn4 has always_inline; compiler already fully inlines all rounds."
    return ""


def progress_optimization_points(text: str) -> list[str]:
    points: list[str] = []
    if re.search(r"SVE vectorization|vectorization_deepen.*SVE|SVE 256-bit", text, re.I):
        status = "SKIPPED" if re.search(r"SVE[\s\S]{0,1200}SKIPPED|SKIPPED[\s\S]{0,1200}SVE", text, re.I) else "Pending"
        reason = extract_skip_reason(text, "SVE")
        suffix = f" with reason: {reason}" if reason and status == "SKIPPED" else ""
        points.append(f"1. SVE vectorization - {status}{suffix}")
    if re.search(r"Round function unroll|round_fn4|throughput-enhancement", text, re.I):
        status = "SKIPPED" if re.search(r"round[\s\S]{0,1200}SKIPPED|SKIPPED[\s\S]{0,1200}round|always_inline", text, re.I) else "Pending"
        reason = extract_skip_reason(text, "round")
        suffix = f" with reason: {reason}" if reason and status == "SKIPPED" else ""
        point_no = 2 if points else 1
        points.append(f"{point_no}. Round function unroll - {status}{suffix}")
    if not points:
        generic = last_match(r"(opt\d+.{0,180}(?:SKIPPED|Pending|APPLIED|FAILED).{0,180})", text)
        if generic:
            points.append("1. " + " ".join(generic.group(1).split())[:300])
    return points[-6:]


def render_table(rows: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return ["  No stage rows detected yet."]
    stage_width = max(len("Stage"), *(len(row[0]) for row in rows))
    status_width = max(len("Status"), *(len(row[1]) for row in rows))
    top = f"  ┌{'─' * (stage_width + 2)}┬{'─' * (status_width + 2)}┐"
    header = f"  │ {'Stage'.center(stage_width)} │ {'Status'.center(status_width)} │"
    sep = f"  ├{'─' * (stage_width + 2)}┼{'─' * (status_width + 2)}┤"
    bottom = f"  └{'─' * (stage_width + 2)}┴{'─' * (status_width + 2)}┘"
    lines = [top, header, sep]
    for stage, status in rows:
        lines.append(f"  │ {stage.ljust(stage_width)} │ {status.ljust(status_width)} │")
    lines.append(bottom)
    return lines


def render_progress_summary(
    project_path: Path,
    log_dir: Path,
    phase: str,
    started: float,
    last_output: float,
    reply_counts: dict[str, int],
    final_seen: bool,
    buffer: str,
) -> str:
    text = clean_terminal_text(buffer)
    target_name = project_path.name
    elapsed = int(time.monotonic() - started)
    idle = int(time.monotonic() - last_output)
    rows = progress_stage_rows(text, final_seen)
    points = progress_optimization_points(text)
    counts = current_task_counts(text)
    stage_snapshot = "; ".join(f"{stage}={status}" for stage, status in rows) or "not visible yet"
    point_snapshot = "; ".join(points) if points else "none reported yet"
    if counts:
        task_snapshot = (
            f"{counts['total']} tasks "
            f"({counts['done']} done, {counts['in_progress']} in progress, {counts['pending']} pending)"
        )
    else:
        task_snapshot = "not visible yet"
    lines = [
        "",
        "Worker status update:",
        "",
        f"  {target_name} Target Progress",
        "",
        *render_table(rows),
        "",
        "  Optimization Points Identified:",
        "",
    ]
    if points:
        for point in points:
            lines.append(f"  {point}")
    else:
        lines.append("  No optimization points reported yet.")
    lines.extend(["", f"  Phase: {phase}"])
    if counts:
        lines.append(
            "  Current Task Count: "
            f"{counts['total']} tasks ({counts['done']} done, {counts['in_progress']} in progress, {counts['pending']} pending)"
        )
    else:
        lines.append("  Current Task Count: not visible yet")
    lines.append(f"  Elapsed: {elapsed}s; idle: {idle}s; final marker seen: {str(final_seen).lower()}")
    lines.append(f"  Auto replies: {json.dumps(reply_counts, ensure_ascii=False)}")
    lines.append(f"  Log dir: {log_dir}")
    excerpt = progress_excerpt(buffer, 220)
    if excerpt:
        lines.extend(["", f"  Recent worker output: {excerpt}"])
    lines.extend(
        [
            "",
            f"  Progress snapshot: phase={phase}; tasks={task_snapshot}; final_marker={str(final_seen).lower()}",
            f"  Progress snapshot: stages={stage_snapshot}",
            f"  Progress snapshot: points={point_snapshot}",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def infer_phase(text: str, fallback: str) -> str:
    cleaned = clean_terminal_text(text)
    checks = [
        ("claude_auto_update", r"Auto-updating|npm install .*claude-code"),
        ("pipeline_complete", r"Pipeline\s+Complete|SESSION_COMPLETE|BATCH_END|SESSION_END"),
        ("final_wrap_up", r"final pipeline wrap-up|final batch-compliant summary|Completing the pipeline"),
        ("verification", r"Functional test passed|VerifyOptimization|验证"),
        ("optimization_point", r"Optimization Points|优化点|DecideOptimization|ApplyOptimization"),
        ("hotspot_analysis", r"热点分析|AnalyzeHotspot|SubTask"),
        ("project_preparation", r"PrepareProject|准备项目|收集优化目标信息"),
    ]
    for phase, pattern in checks:
        if re.search(pattern, cleaned, re.I):
            return phase
    return fallback


def write_progress(
    log_dir: Path,
    project_path: Path,
    status: str,
    phase: str,
    started: float,
    last_output: float,
    reply_counts: dict[str, int],
    final_seen: bool,
    message: str,
    buffer: str = "",
    exit_code: int | None = None,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
) -> None:
    now = time.monotonic()
    text = clean_terminal_text(buffer)
    rows = progress_stage_rows(text, final_seen)
    points = progress_optimization_points(text)
    counts = current_task_counts(text)
    data = {
        "updated_at": utc_now(),
        "status": status,
        "phase": phase,
        "project_path": str(project_path),
        "log_dir": str(log_dir),
        "elapsed_seconds": int(now - started),
        "idle_seconds": int(now - last_output),
        "reply_counts": reply_counts,
        "final_marker_seen": final_seen,
        "exit_code": exit_code,
        "error": error,
        "message": message,
        "stages": [{"stage": stage, "status": stage_status} for stage, stage_status in rows],
        "optimization_points": points,
        "task_counts": counts,
    }
    if usage:
        data["usage"] = usage
    write_json_atomic(log_dir.parent / "progress.json", data)


def print_progress(
    log_dir: Path,
    status: str,
    phase: str,
    started: float,
    last_output: float,
    reply_counts: dict[str, int],
    final_seen: bool,
    message: str,
) -> None:
    now = time.monotonic()
    safe_message = " ".join(message.split())[-180:]
    print(
        "[batch-progress] "
        f"ts={utc_now()} status={status} phase={phase} "
        f"elapsed={int(now - started)}s idle={int(now - last_output)}s "
        f"final_marker={str(final_seen).lower()} replies={json.dumps(reply_counts, ensure_ascii=False)} "
        f"log_dir={log_dir} message={safe_message}",
        flush=True,
    )


def resolve_command(command: list[str], env: dict[str, str]) -> list[str]:
    if not command:
        command = ["claude"]
    executable = command[0]
    if os.sep in executable:
        if not os.access(executable, os.X_OK):
            raise FileNotFoundError(f"command is not executable: {executable}")
        return command
    if executable == "claude":
        claude_bin = env.get("CLAUDE_BIN")
        if claude_bin and os.access(claude_bin, os.X_OK):
            return [claude_bin] + command[1:]
    resolved = shutil.which(executable, path=env.get("PATH"))
    if resolved:
        return [resolved] + command[1:]
    raise FileNotFoundError(f"command not found: {executable}; searched PATH={env.get('PATH', '')}")


def load_answer_bank(path: Path | None) -> str:
    if not path:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def answer_bank_value(answer_bank: str, keys: list[str]) -> str:
    for key in keys:
        patterns = [
            rf"^\s*-\s*{re.escape(key)}\s*:\s*(.+?)\s*$",
            rf"^\s*{re.escape(key)}\s*[：:]\s*(.+?)\s*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, answer_bank, re.M)
            if not match:
                continue
            value = match.group(1).strip().strip("`")
            if value and "worker Claude should select" not in value:
                return value
    return ""


def answer_bank_test_method(answer_bank: str) -> str:
    return answer_bank_value(answer_bank, ["recommended_test_method", "测试命令"])


def answer_bank_exec_method(answer_bank: str) -> str:
    return answer_bank_value(
        answer_bank,
        ["recommended_benchmark_command", "benchmark_command", "recommended_test_method", "测试命令"],
    )


def max_repeats_for_key(key: str, default_max_prompt_repeats: int) -> int:
    if key == "nudge_continue":
        return 6
    if key == "trust_workspace":
        return 20
    if key == "recap_continue":
        return 10
    return default_max_prompt_repeats


def answer_for(buffer: str, answer_bank: str, reply_counts: dict[str, int]) -> tuple[str, str] | None:
    text = clean_terminal_text(buffer)
    recent = text[-RECENT_CONTEXT_CHARS:]
    prompt_area = text[-PROMPT_AREA_CHARS:]
    explicit_input_prompt = bool(USER_INPUT_PROMPT_RE.search(prompt_area))
    selection_ui = bool(SELECTION_UI_RE.search(prompt_area) or explicit_input_prompt)
    agent_status_ui = bool(AGENT_STATUS_UI_RE.search(prompt_area))
    settings_warning = bool(SETTINGS_WARNING_RE.search(prompt_area))
    worker_busy = bool(WORKER_BUSY_RE.search(prompt_area))

    if RECAP_PAUSE_RE.search(prompt_area) and not worker_busy:
        return ("recap_continue", "继续\n")

    if settings_warning and not worker_busy:
        return ("settings_warning_continue", "\n")

    if selection_ui and worker_busy and not explicit_input_prompt:
        return None

    if re.search(
        r"(Quick\s+safety\s+check|Security\s*guide|Do\s+you\s+trust\s+.*folder|trust\s+this\s+folder|I\s+trust\s+this\s+folder|Itrustthisfolder)",
        prompt_area,
        re.I,
    ):
        return ("trust_workspace", "1\n")

    ready_prompt = re.search(
        r"(What would you like|How can I help|Try\s*\"|Try\"|foragents|cwd|❯|>)",
        prompt_area,
        re.I,
    )
    if ready_prompt and not selection_ui and "start_pipeline" not in reply_counts:
        return ("start_pipeline", "/kpbot-code-optimizer\n")

    if selection_ui and re.search(r"(运行模式|pipeline_mode|自动模式|collaboration)", prompt_area, re.I):
        return ("mode_auto", "\x1b[B\n")

    permissions_prompt = re.search(
        r"(环境预检|开启\s*sandbox|配置\s*permissions|Configure\s+permissions|sandbox\s*未开启)",
        prompt_area,
        re.I,
    )
    status_line_only = re.search(r"bypass\s*permissions\s*on", prompt_area, re.I)
    if selection_ui and permissions_prompt and not status_line_only:
        return ("permissions", "\x1b[B\n")

    if selection_ui and re.search(r"(目标类型|优化目标类型|函数优化|用例优化)", prompt_area, re.I):
        if "选择：用例优化" in answer_bank:
            return ("target_type", "用例优化\n")
        return ("target_type", "\n")

    if selection_ui and re.search(r"(代码信息|路径和函数名|帮我分析整个模块)", prompt_area, re.I):
        return ("code_info", "\n")

    if selection_ui and re.search(r"(测试方法|测试用例|没有测试用例)", prompt_area, re.I):
        test_method = answer_bank_test_method(answer_bank)
        return ("test_method", f"{test_method}\n" if test_method else "\n")

    if selection_ui and re.search(r"(执行方法|执行命令|ctest)", prompt_area, re.I):
        exec_method = answer_bank_exec_method(answer_bank)
        return ("exec_method", f"{exec_method}\n" if exec_method else "\n")

    if selection_ui and not agent_status_ui and re.search(
        r"(补充优化点|勾选未命中|同意.*继续|不同意.*重做|是否.*继续.*下一步|确认.*继续.*下一步)",
        prompt_area,
        re.I,
    ):
        return ("accept_stage", "同意，继续下一步。\n")

    if selection_ui and re.search(r"(调用上下文|AnalyzeCallerContext)", prompt_area, re.I):
        return ("skip_caller", "\x1b[B\n")

    if selection_ui and re.search(r"(是否继续|下一轮|自动继续|继续下一轮|停止|终止|retry|resume|重试|继续)", prompt_area, re.I):
        if "自动继续" in prompt_area:
            return ("continue", "\x1b[B\x1b[B\n")
        return ("continue", "\n")

    if selection_ui and not agent_status_ui and re.search(
        r"(确认|Proceed|continue|yes/no|yes|no|是否|选择|请输入|回复)",
        prompt_area,
        re.I,
    ):
        return ("generic_continue", "\n")

    if ready_prompt and "start_pipeline" in reply_counts and not selection_ui:
        if reply_counts.get("nudge_continue", 0) >= 3:
            return (
                "nudge_continue",
                "继续；如果流水线已经完成，请写入 optimization_reports/run_*/FINAL_SUMMARY.md，"
                "同时输出 SESSION_COMPLETE / BATCH_END / SESSION_END 后退出。\n",
            )
        return ("nudge_continue", "继续\n")

    return None


def terminate_process(proc: subprocess.Popen[Any], sig: int = signal.SIGTERM) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=8)


def marker_json_complete(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    for key in ("pipeline_status", "target_status", "status", "result", "batch_status"):
        value = str(data.get(key, "")).strip().lower()
        if value in COMPLETE_STATUSES or value.startswith("complete"):
            return True
    return False


def report_text_complete(path: Path) -> bool:
    try:
        text = clean_terminal_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    if FINAL_RE.search(text):
        return True
    return bool(
        re.search(
            r"(Pipeline\s+Complete|Final\s+Verdict|Generated\s+by\s+KunpengAccelerationLibOptimization|"
            r"No optimization possible|No code changes required|practical maximum)",
            text,
            re.I,
        )
    )


def completion_marker_exists(project_path: Path, log_dir: Path) -> bool:
    reports = project_path / "optimization_reports"
    if reports.exists():
        for path in reports.rglob("*"):
            if not path.is_file():
                continue
            lowered = path.name.lower()
            if lowered in FINAL_REPORT_NAMES:
                return True
            if lowered == "batch_result.json" and marker_json_complete(path):
                return True
            if lowered == "optimization_summary.md" and report_text_complete(path):
                return True
    marker_paths = [
        project_path / ".batch_optimize_result.json",
        log_dir / "final_result.json",
        log_dir.parent / "TARGET_COMPLETE.json",
        log_dir.parent / ".batch_optimize_result.json",
        log_dir.parent.parent / "batch_summary.json",
    ]
    for path in marker_paths:
        if path.exists() and marker_json_complete(path):
            return True
    for path in [project_path / ".batch_optimize_result.md", log_dir.parent.parent / "BATCH_SUMMARY.md"]:
        if path.exists() and FINAL_RE.search(clean_terminal_text(path.read_text(encoding="utf-8", errors="replace"))):
            return True
    return False


def run_session(args: argparse.Namespace) -> dict[str, Any]:
    project_path = Path(args.project_path).expanduser().resolve()
    log_dir = Path(args.log_dir).expanduser().resolve()
    answer_bank = load_answer_bank(Path(args.answer_bank_file).expanduser().resolve() if args.answer_bank_file else None)
    env = child_env(args.claude_bin)
    command = resolve_command(args.command or ["claude"], env)

    session = SessionManager(project_path, log_dir, command, args)
    recorder = session.recorder()
    session.write_running_result()

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

    buffer = ""
    started = time.monotonic()
    last_output = time.monotonic()
    last_semantic_output = last_output
    last_semantic_excerpt = "worker started"
    last_reply_at = 0.0
    reply_counts: dict[str, int] = {}
    final_seen_at: float | None = None
    exit_sent = False
    status = "failed"
    error: str | None = None
    exit_code: int | None = None
    phase = "starting"
    last_progress_at = 0.0
    worker_input_tokens = 0
    worker_output_tokens = 0
    driver_prompt_tokens, token_method = estimate_token_count(answer_bank)
    context_window_peak = driver_prompt_tokens
    soft_rollover_sent_at: float | None = None
    soft_rollover_trigger_tokens: int | None = None

    def usage_snapshot() -> dict[str, Any]:
        return {
            "attempt_index": args.attempt_index,
            "worker_input_tokens_estimate": worker_input_tokens,
            "worker_output_tokens_estimate": worker_output_tokens,
            "transcript_tokens_estimate": worker_input_tokens + worker_output_tokens,
            "driver_prompt_tokens_estimate": driver_prompt_tokens,
            "context_window_peak_estimate": context_window_peak,
            "method": token_method,
            "is_exact": False,
        }

    def add_worker_input(text: str) -> None:
        nonlocal worker_input_tokens, context_window_peak, token_method
        tokens, method = estimate_token_count(text)
        token_method = method
        worker_input_tokens += tokens
        context_window_peak = max(context_window_peak, driver_prompt_tokens + worker_input_tokens + worker_output_tokens)

    def add_worker_output(text: str) -> None:
        nonlocal worker_output_tokens, context_window_peak, token_method
        tokens, method = estimate_token_count(text)
        token_method = method
        worker_output_tokens += tokens
        context_window_peak = max(context_window_peak, driver_prompt_tokens + worker_input_tokens + worker_output_tokens)

    def emit_progress(status_label: str = "running", force: bool = False, message: str | None = None) -> None:
        nonlocal last_progress_at, phase
        now_inner = time.monotonic()
        phase = infer_phase(buffer, phase)
        progress_message = message or progress_excerpt(buffer) or "waiting for worker Claude output"
        write_progress(
            log_dir=log_dir,
            project_path=project_path,
            status=status_label,
            phase=phase,
            started=started,
            last_output=last_output,
            reply_counts=reply_counts,
            final_seen=final_seen_at is not None,
            message=progress_message,
            buffer=buffer,
            exit_code=exit_code,
            error=error,
            usage=usage_snapshot(),
        )
        if force or now_inner - last_progress_at >= args.progress_interval_seconds:
            print(
                render_progress_summary(
                    project_path=project_path,
                    log_dir=log_dir,
                    phase=phase,
                    started=started,
                    last_output=last_output,
                    reply_counts=reply_counts,
                    final_seen=final_seen_at is not None,
                    buffer=buffer,
                ),
                flush=True,
            )
            last_progress_at = now_inner

    emit_progress(force=True, message="worker Claude launched")

    try:
        while True:
            now = time.monotonic()
            if now - started > args.timeout_seconds:
                error = f"hard timeout after {args.timeout_seconds} seconds"
                terminate_process(proc)
                break
            if now - last_output > args.idle_timeout_seconds and final_seen_at is None:
                error = f"idle timeout after {args.idle_timeout_seconds} seconds without worker output"
                terminate_process(proc)
                break
            if now - last_semantic_output > args.idle_timeout_seconds and final_seen_at is None:
                status = "semantic_idle_timeout"
                error = (
                    f"semantic idle timeout after {args.idle_timeout_seconds} seconds without meaningful "
                    f"worker progress; last semantic output: {last_semantic_excerpt[:240]}"
                )
                terminate_process(proc)
                break

            if (
                final_seen_at is None
                and args.context_hard_limit_tokens > 0
                and context_window_peak >= args.context_hard_limit_tokens
            ):
                status = "context_limit"
                error = f"context hard limit reached at ~{context_window_peak} tokens"
                terminate_process(proc)
                break
            if final_seen_at is None and soft_rollover_sent_at is not None and now - soft_rollover_sent_at > 12:
                status = "context_rollover"
                error = f"context soft limit rollover requested at ~{soft_rollover_trigger_tokens or context_window_peak} tokens"
                terminate_process(proc)
                break

            if final_seen_at is None and completion_marker_exists(project_path, log_dir):
                final_seen_at = now
                recorder.event("batch_driver", "Final completion marker detected; requesting Claude exit.\n")
                emit_progress(force=True, message="final completion marker detected")

            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    data = b""
                if data:
                    last_output = now
                    recorder.output(data)
                    chunk = safe_text(data)
                    if is_meaningful_worker_output(chunk):
                        last_semantic_output = now
                        last_semantic_excerpt = clean_terminal_text(chunk)[-500:] or "meaningful output"
                    add_worker_output(chunk)
                    buffer += chunk
                    buffer = buffer[-30000:]
                    if final_seen_at is None and context_ui_limit_reached(chunk + "\n" + buffer[-5000:]):
                        status = "context_limit"
                        error = "Claude UI reported context exhausted before final-result marker"
                        terminate_process(proc)
                        break
                    # Keep accepting --allow-transcript-final-marker for old batch driver
                    # processes, but do not let prompt text or status chatter end a
                    # worker. Completion must come from a file marker.
                else:
                    break

            if final_seen_at is not None:
                if not exit_sent and now - final_seen_at > 3:
                    msg = "/exit\n"
                    os.write(master_fd, msg.replace("\n", "\r").encode("utf-8"))
                    recorder.input(msg)
                    add_worker_input(msg)
                    exit_sent = True
                if exit_sent and now - final_seen_at > 20:
                    status = "completed"
                    terminate_process(proc)
                    break
            elif now - last_reply_at > args.min_reply_interval_seconds:
                if (
                    args.context_soft_limit_tokens > 0
                    and context_window_peak >= args.context_soft_limit_tokens
                    and soft_rollover_sent_at is None
                ):
                    message = (
                        "上下文接近 batch 软限制。请立即写入 optimization_reports/run_*/CHECKPOINT.md，"
                        "用不超过 800 字总结已完成阶段、当前 blocker、已应用 patch、已验证证据和下一步，"
                        "然后停止继续展开；外层 batch driver 会启动新 worker 从 checkpoint 恢复。\n"
                    )
                    os.write(master_fd, message.replace("\n", "\r").encode("utf-8"))
                    recorder.input(message)
                    add_worker_input(message)
                    last_reply_at = now
                    soft_rollover_sent_at = now
                    soft_rollover_trigger_tokens = context_window_peak
                    emit_progress(force=True, message="context soft limit rollover requested")
                    answer = None
                else:
                    answer = answer_for(buffer, answer_bank, reply_counts)
                if answer:
                    key, message = answer
                    if key == "nudge_continue" and now - last_output < args.nudge_after_seconds:
                        answer = None
                    else:
                        reply_counts[key] = reply_counts.get(key, 0) + 1
                        max_repeats = max_repeats_for_key(key, args.max_prompt_repeats)
                        if key in {"test_method", "exec_method"} and message.strip():
                            max_repeats = max(max_repeats, 6)
                        if reply_counts[key] > max_repeats:
                            error = f"prompt/action repeated too many times: {key}"
                            terminate_process(proc)
                            break
                        os.write(master_fd, message.replace("\n", "\r").encode("utf-8"))
                        recorder.input(message)
                        add_worker_input(message)
                        last_reply_at = now
                        emit_progress(force=True, message=f"auto reply sent: {key}")

            exit_code = proc.poll()
            if exit_code is not None:
                if final_seen_at is not None:
                    status = "completed"
                else:
                    error = f"worker Claude exited before final result with code {exit_code}"
                break
            emit_progress()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            terminate_process(proc)
        exit_code = proc.poll()
        if status != "completed" and error is None and final_seen_at is None:
            error = "worker Claude ended without final-result marker"
        if final_seen_at is not None and status != "completed" and error is None:
            status = "completed"
        emit_progress(status_label=status, force=True, message=error or "worker session ended")
        recorder.close(status, exit_code, error)

    ended_at = utc_now()
    duration_seconds = int(time.monotonic() - started)
    result = {
        "status": status,
        "error": error,
        "exit_code": exit_code,
        "log_dir": str(log_dir),
        "final_marker_seen": final_seen_at is not None,
        "reply_counts": reply_counts,
        "command": command,
        "started_at": meta["started_at"],
        "ended_at": ended_at,
        "updated_at": ended_at,
        "attempt_index": args.attempt_index,
        "termination_reason": "completed" if status == "completed" else (error or status),
        "usage": usage_snapshot(),
        "timing": {
            "started_at": meta["started_at"],
            "ended_at": ended_at,
            "duration_seconds": duration_seconds,
            "idle_seconds_final": int(time.monotonic() - last_output),
        },
    }
    write_json_atomic(log_dir / "auto_result.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--answer-bank-file")
    parser.add_argument("--claude-bin")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--idle-timeout-seconds", type=int, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--max-prompt-repeats", type=int, default=DEFAULT_MAX_PROMPT_REPEATS)
    parser.add_argument("--min-reply-interval-seconds", type=float, default=DEFAULT_MIN_REPLY_INTERVAL_SECONDS)
    parser.add_argument("--nudge-after-seconds", type=float, default=DEFAULT_NUDGE_AFTER_SECONDS)
    parser.add_argument("--progress-interval-seconds", type=float, default=DEFAULT_PROGRESS_INTERVAL_SECONDS)
    parser.add_argument("--attempt-index", type=int, default=DEFAULT_ATTEMPT_INDEX)
    parser.add_argument("--context-soft-limit-tokens", type=int, default=DEFAULT_CONTEXT_SOFT_LIMIT_TOKENS)
    parser.add_argument("--context-hard-limit-tokens", type=int, default=DEFAULT_CONTEXT_HARD_LIMIT_TOKENS)
    parser.add_argument("--transcript-tail-lines", type=int, default=DEFAULT_TRANSCRIPT_TAIL_LINES)
    parser.add_argument("--allow-transcript-final-marker", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args


def main() -> int:
    args = parse_args()
    try:
        result = run_session(args)
    except FileNotFoundError as exc:
        log_dir = Path(args.log_dir).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        result = {
            "status": "failed",
            "error": str(exc),
            "exit_code": 127,
            "log_dir": str(log_dir),
            "final_marker_seen": False,
            "reply_counts": {},
            "command": args.command or ["claude"],
        }
        (log_dir / "auto_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(str(exc), file=sys.stderr)
        return 127
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
