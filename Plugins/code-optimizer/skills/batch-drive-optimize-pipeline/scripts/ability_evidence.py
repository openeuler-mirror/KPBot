"""Record optimization ability evidence from batch-drive structured artifacts."""

from __future__ import annotations

from collections import Counter
import json
import re
from pathlib import Path
from typing import Any


SUCCESS_STATUSES = {"applied_verified", "analysis_only", "complete_no_optimization"}
NUMERIC_TOKEN_PATTERN = r"[-+]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][-+]?\d+)?"
NUMERIC_TOKEN_RE = re.compile(NUMERIC_TOKEN_PATTERN)
SIMPLE_NUMERIC_RE = re.compile(
    rf"^\s*(?:[A-Za-z_][A-Za-z0-9_ ./-]*[:=]\s*)?({NUMERIC_TOKEN_PATTERN})\s*(?:x|%|percent)?\s*$",
    re.I,
)
FAILURE_STATUSES = {
    "driver_failed",
    "pipeline_incomplete",
    "baseline_blocked",
    "artifact_error",
    "report_inconsistent",
}


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def number_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if len(NUMERIC_TOKEN_RE.findall(value)) != 1:
            return None
        match = SIMPLE_NUMERIC_RE.match(value)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def performance_speedup_from_summary(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("speedup", "speedup_ratio", "x_speedup"):
            speedup = number_value(value.get(key))
            if speedup and speedup > 0:
                return speedup
        for key in ("improvement_percent", "performance_improvement_percent", "improvement"):
            pct = number_value(value.get(key))
            if pct is not None:
                return 1.0 + pct / 100.0
        for item in value.values():
            speedup = performance_speedup_from_summary(item)
            if speedup:
                return speedup
    elif isinstance(value, list):
        for item in value:
            speedup = performance_speedup_from_summary(item)
            if speedup:
                return speedup
    return None


def performance_speedup(result: dict[str, Any], target_out: Path | None = None) -> float | None:
    structured = result.get("structured_evidence") if isinstance(result.get("structured_evidence"), dict) else {}
    speedup = performance_speedup_from_summary(structured.get("performance_summary"))
    if speedup:
        return speedup
    speedup = performance_speedup_from_summary(structured)
    if speedup:
        return speedup

    summary_path = target_out / "final_summary.md" if target_out else None
    text = summary_path.read_text(encoding="utf-8", errors="replace") if summary_path and summary_path.exists() else ""
    patterns = [
        rf"speedup\s*[:=]?\s*({NUMERIC_TOKEN_PATTERN})\s*x",
        rf"加速比\s*[:=：]?\s*({NUMERIC_TOKEN_PATTERN})",
        rf"性能(?:提升|改进|下降|回归)?\s*[:=：]?\s*({NUMERIC_TOKEN_PATTERN})\s*%",
        rf"performance[^\n]{{0,40}}?\s*({NUMERIC_TOKEN_PATTERN})\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = float(match.group(1))
        return value if "x" in match.group(0).lower() or "加速比" in match.group(0) else 1.0 + value / 100.0
    return None


def event_counts(events: Any) -> dict[str, int]:
    if not isinstance(events, list):
        return {}
    severities = Counter(str(event.get("severity") or "unknown") for event in events if isinstance(event, dict))
    return dict(sorted(severities.items()))


def summarize_target_evidence(result: dict[str, Any]) -> dict[str, Any]:
    target_out = Path(str(result.get("target_out") or ".")) if result.get("target_out") else None
    completion_gate = result.get("completion_gate") if isinstance(result.get("completion_gate"), dict) else {}
    if not completion_gate and target_out:
        completion_gate = read_json(target_out / "completion_gate.json")
    trace = read_json(Path(str(result.get("trace_grade")))) if result.get("trace_grade") else {}
    status = str(result.get("quality_status") or result.get("status") or "unknown")
    patch_info = result.get("patch_info") if isinstance(result.get("patch_info"), dict) else {}
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}

    patch_collected = bool(completion_gate.get("patch_collected") or int(patch_info.get("final_patch_bytes") or 0) > 0)
    functional_verified = bool(completion_gate.get("functional_verified"))
    performance_measured = bool(completion_gate.get("performance_measured"))
    required_stages_seen = bool(completion_gate.get("required_stages_seen"))
    pipeline_reached_final = bool(completion_gate.get("pipeline_reached_final_report") or result.get("reached_final_summary"))
    artifact_consistent = bool(completion_gate.get("artifact_consistent"))
    patch_hygiene = bool(completion_gate.get("patch_hygiene_passed", True))
    source_unchanged = bool(result.get("source_unchanged", True))
    speedup = performance_speedup(result, target_out)

    risk_flags: list[str] = []
    if status in FAILURE_STATUSES:
        risk_flags.append(status)
    if status == "applied_unverified":
        risk_flags.append("applied_unverified")
    if not pipeline_reached_final:
        risk_flags.append("missing_final_report")
    if not required_stages_seen:
        risk_flags.append("stage_evidence_incomplete")
    if not artifact_consistent:
        risk_flags.append("artifact_inconsistent")
    if not patch_hygiene:
        risk_flags.append("patch_hygiene_failed")
    if patch_collected and not functional_verified:
        risk_flags.append("patch_without_functional_verification")
    if patch_collected and not performance_measured:
        risk_flags.append("patch_without_performance_measurement")
    if not patch_collected and status not in {"analysis_only", "complete_no_optimization"}:
        risk_flags.append("no_code_change")
    if not source_unchanged:
        risk_flags.append("source_repo_changed")

    return {
        "target_id": result.get("target_id"),
        "status": status,
        "evidence": {
            "pipeline_reached_final_report": pipeline_reached_final,
            "required_stages_seen": required_stages_seen,
            "patch_collected": patch_collected,
            "functional_verified": functional_verified,
            "performance_measured": performance_measured,
            "artifact_consistent": artifact_consistent,
            "patch_hygiene_passed": patch_hygiene,
            "source_unchanged": source_unchanged,
            "speedup": speedup,
            "usage": usage,
            "timing": timing,
        },
        "trace": {
            "status": trace.get("status"),
            "event_counts": event_counts(trace.get("events")),
            "missing_preparation_stages": trace.get("missing_preparation_stages") or [],
            "weak_preparation_stages": trace.get("weak_preparation_stages") or [],
            "missing_optimization_stages": trace.get("missing_optimization_stages") or [],
            "weak_optimization_stages": trace.get("weak_optimization_stages") or [],
        },
        "risk_flags": sorted(set(risk_flags)),
    }


def render_target_evidence(report: dict[str, Any]) -> str:
    evidence = report.get("evidence") or {}
    trace = report.get("trace") or {}
    risk_flags = report.get("risk_flags") or []
    lines = [
        f"# Ability Evidence: {report.get('target_id')}",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Speedup: `{evidence.get('speedup')}`",
        f"- Risk flags: `{', '.join(risk_flags) or 'none'}`",
        "",
        "| Evidence | Present |",
        "|---|---:|",
    ]
    for key in [
        "pipeline_reached_final_report",
        "required_stages_seen",
        "patch_collected",
        "functional_verified",
        "performance_measured",
        "artifact_consistent",
        "patch_hygiene_passed",
        "source_unchanged",
    ]:
        lines.append(f"| `{key}` | `{bool(evidence.get(key))}` |")
    lines.extend(["", "## Trace", ""])
    lines.append(f"- Status: `{trace.get('status')}`")
    lines.append(f"- Event counts: `{json.dumps(trace.get('event_counts') or {}, ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- Missing preparation stages: `{', '.join(trace.get('missing_preparation_stages') or []) or 'none'}`")
    lines.append(f"- Weak preparation stages: `{', '.join(trace.get('weak_preparation_stages') or []) or 'none'}`")
    lines.append(f"- Missing optimization stages: `{', '.join(trace.get('missing_optimization_stages') or []) or 'none'}`")
    lines.append(f"- Weak optimization stages: `{', '.join(trace.get('weak_optimization_stages') or []) or 'none'}`")
    lines.append("")
    return "\n".join(lines)


def write_target_evidence(result: dict[str, Any]) -> dict[str, Any]:
    report = summarize_target_evidence(result)
    target_out = Path(str(result.get("target_out") or "."))
    write_json(target_out / "ability_evidence.json", report)
    (target_out / "ability_evidence.md").write_text(render_target_evidence(report), encoding="utf-8")
    return report


def aggregate_batch_evidence(target_reports: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status") or "unknown") for item in target_reports)
    risk_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    for item in target_reports:
        for flag in item.get("risk_flags") or []:
            risk_counts[str(flag)] += 1
        evidence = item.get("evidence") or {}
        for key, value in evidence.items():
            if isinstance(value, bool) and value:
                evidence_counts[key] += 1
    return {
        "status": "completed" if target_reports else "empty",
        "target_count": len(target_reports),
        "status_counts": dict(sorted(status_counts.items())),
        "risk_flag_counts": dict(sorted(risk_counts.items())),
        "evidence_counts": dict(sorted(evidence_counts.items())),
        "targets": target_reports,
    }


def render_batch_evidence(batch_report: dict[str, Any]) -> str:
    lines = [
        "# Batch Optimization Ability Evidence",
        "",
        f"- Status: `{batch_report.get('status')}`",
        f"- Target count: `{batch_report.get('target_count')}`",
        f"- Status counts: `{json.dumps(batch_report.get('status_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
        f"- Risk flag counts: `{json.dumps(batch_report.get('risk_flag_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
        "",
        "| Target | Status | Speedup | Risk flags |",
        "|---|---|---:|---|",
    ]
    for item in batch_report.get("targets") or []:
        evidence = item.get("evidence") or {}
        risk_flags = ", ".join(item.get("risk_flags") or []) or "none"
        lines.append(
            f"| `{item.get('target_id')}` | `{item.get('status')}` | `{evidence.get('speedup')}` | `{risk_flags}` |"
        )
    lines.append("")
    return "\n".join(lines)


def write_batch_evidence(out: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    target_reports = [write_target_evidence(result) for result in results]
    batch_report = aggregate_batch_evidence(target_reports)
    write_json(out / "ability_evidence.json", batch_report)
    (out / "ability_evidence.md").write_text(render_batch_evidence(batch_report), encoding="utf-8")
    return batch_report
