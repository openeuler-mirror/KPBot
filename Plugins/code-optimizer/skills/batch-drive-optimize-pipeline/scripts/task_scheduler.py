"""Target selection helpers for the batch-drive pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
import re


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "target"


def target_manifest_id(target: dict[str, Any]) -> str:
    raw_id = target.get("id")
    if raw_id:
        return slug(str(raw_id))
    project_path = target.get("project_path")
    if project_path:
        return slug(Path(str(project_path)).name)
    return "target"


def split_target_selector(selector: str | None) -> list[str]:
    if not selector:
        return []
    return [slug(part) for part in re.split(r"[\s,]+", selector) if part.strip()]


def filter_manifest_targets(targets: list[dict[str, Any]], selector: str | None) -> list[dict[str, Any]]:
    requested = split_target_selector(selector)
    if not requested:
        return targets
    available = [target_manifest_id(target) for target in targets]
    available_set = set(available)
    missing = [target_id for target_id in requested if target_id not in available_set]
    if missing:
        raise SystemExit(
            "unknown target id(s): "
            + ", ".join(missing)
            + "; available targets: "
            + ", ".join(available)
        )
    requested_set = set(requested)
    return [target for target in targets if target_manifest_id(target) in requested_set]


def print_target_list(targets: list[dict[str, Any]]) -> None:
    for target in targets:
        print(f"{target_manifest_id(target)}\t{target.get('project_path', '')}")


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class TaskScheduler:
    """Serial target scheduler and target-local driver failure recorder."""

    def __init__(
        self,
        *,
        targets: list[dict[str, Any]],
        out: Path,
        run_cfg: dict[str, Any],
        legacy_driver: bool,
        managed_driver_version: str,
        progress_renderer: Callable[[Path, list[dict[str, Any]], str | None], None],
        now: Callable[[], str],
        failure_exceptions: tuple[type[BaseException], ...],
    ) -> None:
        self.targets = targets
        self.out = out
        self.run_cfg = run_cfg
        self.legacy_driver = legacy_driver
        self.managed_driver_version = managed_driver_version
        self.progress_renderer = progress_renderer
        self.now = now
        self.failure_exceptions = failure_exceptions

    def validate_serial(self) -> None:
        if int(self.run_cfg.get("max_parallel") or 1) != 1:
            raise SystemExit("first version supports only max_parallel=1")

    def run_serial(
        self,
        run_target: Callable[[dict[str, Any], Callable[[], None]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        self.validate_serial()
        results: list[dict[str, Any]] = []
        total_targets = len(self.targets)
        for index, target in enumerate(self.targets, start=1):
            target_id = target_manifest_id(target)
            print(f"\n[batch] target {index}/{total_targets}: {target_id}", flush=True)
            progress_callback = lambda active_id=target_id: self.progress_renderer(self.out, self.targets, active_id)
            try:
                results.append(run_target(target, progress_callback))
            except self.failure_exceptions as exc:
                result = self.record_driver_failure(target, exc)
                results.append(result)
                print(f"[batch] target {index}/{total_targets} failed: {target_id}: {exc}", flush=True)
                self.progress_renderer(self.out, self.targets, None)
        return results

    def record_driver_failure(self, target: dict[str, Any], exc: BaseException) -> dict[str, Any]:
        target_id = target_manifest_id(target)
        target_out = self.out / "targets" / target_id
        target_out.mkdir(parents=True, exist_ok=True)
        usage = {
            "worker_input_tokens_estimate": 0,
            "worker_output_tokens_estimate": 0,
            "transcript_tokens_estimate": 0,
            "driver_prompt_tokens_estimate": 0,
            "context_window_peak_estimate": 0,
            "attempts": [],
            "method": "not_available",
            "is_exact": False,
        }
        timing = {
            "started_at": self.now(),
            "ended_at": self.now(),
            "wall_time_seconds": 0,
            "active_worker_seconds": 0,
            "idle_seconds_total": 0,
            "driver_overhead_seconds": 0,
            "attempt_count": 0,
            "attempts": [],
        }
        gate = {
            "pipeline_reached_final_report": False,
            "required_stages_seen": False,
            "patch_collected": False,
            "functional_verified": False,
            "performance_measured": False,
            "artifact_consistent": False,
            "patch_hygiene_passed": False,
            "evidence_sources": {
                "structured_json": [],
                "batch_result": [],
                "baseline_blocked": [],
                "verification": [],
                "stage_sources": {},
            },
            "status": "driver_failed",
        }
        result = {
            "target_id": target_id,
            "status": "driver_failed",
            "quality_status": "driver_failed",
            "run_status": "failed",
            "pipeline_status": "incomplete",
            "completion_status": "driver_failed",
            "blocker": str(exc),
            "reached_final_summary": False,
            "target_out": str(target_out),
            "usage": usage,
            "usage_path": str(target_out / "usage.json"),
            "timing": timing,
            "timing_path": str(target_out / "timing.json"),
            "completion_gate": gate,
            "completion_gate_path": str(target_out / "completion_gate.json"),
        }
        _write_json_file(target_out / "usage.json", usage)
        _write_json_file(target_out / "timing.json", timing)
        _write_json_file(target_out / "completion_gate.json", gate)
        _write_json_file(target_out / "target_result.json", result)
        _write_json_file(
            target_out / "target_state.json",
            {
                "driver": "legacy" if self.legacy_driver else self.managed_driver_version,
                "target_id": target_id,
                "run_status": "failed",
                "pipeline_status": "incomplete",
                "quality_status": "driver_failed",
                "completion_status": "driver_failed",
                "blocker": str(exc),
            },
        )
        (target_out / "target_report.md").write_text(
            f"# Target Report: {target_id}\n\n- Status: `driver_failed`\n\n## Blocker\n\n{exc}\n",
            encoding="utf-8",
        )
        return result
