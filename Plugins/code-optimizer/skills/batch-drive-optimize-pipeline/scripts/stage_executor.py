"""Stage execution controller for one batch-drive target."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import time


@dataclass(frozen=True)
class StageExecutorConfig:
    target_id: str
    workdir: Path
    target_out: Path
    answer_bank_file: Path
    claude_bin: str
    timeout_minutes: int
    idle_timeout_minutes: int
    progress_interval_seconds: float
    resume_attempts: int
    context_soft_limit_tokens: int
    context_hard_limit_tokens: int
    resume_prompt_max_tokens: int
    transcript_tail_lines: int
    legacy_driver: bool


class StageExecutor:
    def __init__(
        self,
        *,
        invoke_worker: Callable[..., dict[str, Any]],
        attempt_record: Callable[[int, str, int, dict[str, Any]], dict[str, Any]],
        build_resume_prompt: Callable[..., str],
        write_target_state: Callable[[Path, dict[str, Any]], None],
        append_jsonl: Callable[[Path, dict[str, Any]], None],
        utc_now: Callable[[], str],
        progress_callback: Callable[[], None] | None = None,
    ) -> None:
        self.invoke_worker = invoke_worker
        self.attempt_record = attempt_record
        self.build_resume_prompt = build_resume_prompt
        self.write_target_state = write_target_state
        self.append_jsonl = append_jsonl
        self.utc_now = utc_now
        self.progress_callback = progress_callback

    def run(self, config: StageExecutorConfig, attempts_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        runner_result: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        for attempt in range(config.resume_attempts + 1):
            self.write_target_state(
                config.target_out,
                {
                    "run_status": "running",
                    "current_attempt": attempt,
                    "attempts_allowed": config.resume_attempts + 1,
                    "context_soft_limit_tokens": config.context_soft_limit_tokens,
                    "context_hard_limit_tokens": config.context_hard_limit_tokens,
                },
            )
            print(
                f"\n[batch-target] start target={config.target_id} attempt={attempt} "
                f"workdir={config.workdir} target_out={config.target_out}",
                flush=True,
            )
            if self.progress_callback:
                self.progress_callback()
            attempt_started_at = self.utc_now()
            attempt_started_monotonic = time.monotonic()
            runner_result = self.invoke_worker(
                workdir=config.workdir,
                target_out=config.target_out,
                answer_bank_file=config.answer_bank_file,
                claude_bin=config.claude_bin,
                timeout_minutes=config.timeout_minutes,
                idle_timeout_minutes=config.idle_timeout_minutes,
                progress_interval_seconds=config.progress_interval_seconds,
                attempt_index=attempt,
                context_soft_limit_tokens=config.context_soft_limit_tokens,
                context_hard_limit_tokens=config.context_hard_limit_tokens,
                transcript_tail_lines=config.transcript_tail_lines,
                progress_callback=self.progress_callback,
            )
            runner_result["attempt"] = attempt
            attempt_duration = int(time.monotonic() - attempt_started_monotonic)
            record = self.attempt_record(attempt, attempt_started_at, attempt_duration, runner_result)
            attempts.append(record)
            self.append_jsonl(attempts_path, record)
            print(
                f"[batch-target] runner finished target={config.target_id} attempt={attempt} "
                f"status={runner_result.get('status')} final_marker_seen={runner_result.get('final_marker_seen')} "
                f"error={runner_result.get('error') or ''}",
                flush=True,
            )
            if runner_result.get("status") == "completed":
                break
            if attempt < config.resume_attempts:
                self._append_resume_prompt(config, runner_result, attempt)
        return runner_result, attempts

    def _append_resume_prompt(self, config: StageExecutorConfig, runner_result: dict[str, Any], attempt: int) -> None:
        resume_text = (
            "\n恢复提示：继续上一次 /optimize-pipeline 工作流。"
            "请从已有 optimization_reports 和当前仓库状态恢复，完成剩余阶段直到结束报告。\n"
        ) if config.legacy_driver else self.build_resume_prompt(
            target_id=config.target_id,
            workdir=config.workdir,
            target_out=config.target_out,
            runner_result=runner_result,
            max_tokens=config.resume_prompt_max_tokens,
            transcript_tail_lines=config.transcript_tail_lines,
        )
        with config.answer_bank_file.open("a", encoding="utf-8") as handle:
            handle.write(resume_text)
        with (config.workdir / ".batch_optimize_answer_bank.md").open("a", encoding="utf-8") as handle:
            handle.write(resume_text)
        self.write_target_state(
            config.target_out,
            {
                "run_status": "resuming",
                "last_attempt_status": runner_result.get("status"),
                "last_termination_reason": runner_result.get("termination_reason") or runner_result.get("error"),
                "next_attempt": attempt + 1,
            },
        )
