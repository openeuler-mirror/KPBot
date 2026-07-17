#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


REQUIRED_FIELDS = [
    "scenario_name",
    "application_name",
    "workload_type",
    "deployment_topology",
    "overall_progress",
    "workflow_gate_status",
    "current_run_id",
    "current_run_started_at",
    "current_run_manifest",
    "current_evidence_status",
    "service_health_status",
    "service_health_checks",
    "service_health_evidence",
    "test_topology_confidence",
    "test_case_confidence",
    "environment_snapshot",
    "environment_backup_dir",
    "environment_diagnosis",
    "baseline_metrics",
    "baseline_confirmation_status",
    "target_instance_identity",
    "bottleneck_classification",
    "bottleneck_evidence",
    "workflow_trace",
    "workflow_execution_plan",
    "workflow_stage_trace",
    "performance_signal_summary",
    "candidate_skill_list",
    "candidate_pool",
    "optimization_actions",
    "before_after_metrics",
    "improvement_summary",
    "agent_timing_summary",
    "per_skill_timing_summary",
    "optimization_timing",
    "optimization_timing_details",
    "per_skill_iteration_state",
    "selected_optimization_actions",
    "rejected_optimization_actions",
    "review_result",
    "restore_result",
    "next_steps",
    "case_archive_path",
    "historical_records_status",
]

COMPLETED_HARD_REQUIRED_FIELDS = [
    "workflow_execution_plan",
    "workflow_stage_trace",
    "agent_timing_summary",
    "per_skill_timing_summary",
    "optimization_timing",
    "optimization_timing_details",
]

BLOCKED_ALLOWED_EMPTY_FIELDS = {
    "baseline_metrics",
    "bottleneck_evidence",
    "workflow_trace",
    "performance_signal_summary",
    "candidate_skill_list",
    "candidate_pool",
    "optimization_actions",
    "before_after_metrics",
    "workflow_execution_plan",
    "workflow_stage_trace",
    "agent_timing_summary",
    "per_skill_timing_summary",
    "optimization_timing",
    "optimization_timing_details",
    "per_skill_iteration_state",
    "selected_optimization_actions",
    "rejected_optimization_actions",
    "review_result",
    "restore_result",
    "case_archive_path",
}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_timing_jsonl(path, input_path=None):
    if not path:
        return [], ""
    timing_path = Path(path)
    if not timing_path.is_absolute() and not timing_path.exists() and input_path:
        candidate = Path(input_path).parent / timing_path
        if candidate.exists():
            timing_path = candidate
    if not timing_path.exists():
        return [], f"timing_jsonl_path not found: {path}"

    records = []
    for line_number, line in enumerate(timing_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            return records, f"invalid timing JSONL at {timing_path}:{line_number}: {exc}"
    return [normalize_timing_record(record) for record in records], ""


def as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def as_dict(value):
    return value if isinstance(value, dict) else {}


def json_block(value):
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False) + "\n```"


def scalar(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def duration_text(seconds):
    if seconds in (None, ""):
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def number_value(value):
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_timing_record(record):
    normalized = dict(record)
    analysis = number_value(normalized.get("analysis_seconds"))
    implementation = number_value(normalized.get("implementation_seconds"))
    validation = number_value(normalized.get("validation_seconds"))
    total = number_value(normalized.get("total_seconds"))
    if not total:
        total = analysis + implementation + validation

    normalized.setdefault("analysis_seconds", analysis)
    normalized.setdefault("implementation_seconds", implementation)
    normalized.setdefault("validation_seconds", validation)
    normalized.setdefault("total_seconds", total)
    normalized.setdefault("analysis_duration", duration_text(analysis))
    normalized.setdefault("implementation_duration", duration_text(implementation))
    normalized.setdefault("validation_duration", duration_text(validation))
    normalized.setdefault("total_duration", duration_text(total))
    normalized.setdefault("skill_name", "")
    normalized.setdefault("round_name", "")
    normalized.setdefault("status", "recorded")
    normalized.setdefault("evidence_path", "")
    return normalized


def summarize_timing_records(records):
    totals = {
        "analysis_seconds": 0.0,
        "implementation_seconds": 0.0,
        "validation_seconds": 0.0,
        "total_seconds": 0.0,
    }
    for record in records:
        for key in totals:
            totals[key] += number_value(record.get(key))

    summary = dict(totals)
    summary.update({
        "analysis_duration": duration_text(totals["analysis_seconds"]),
        "implementation_duration": duration_text(totals["implementation_seconds"]),
        "validation_duration": duration_text(totals["validation_seconds"]),
        "total_duration": duration_text(totals["total_seconds"]),
        "record_count": len(records),
    })
    return summary


def summarize_per_skill(records):
    grouped = {}
    for record in records:
        skill_name = record.get("skill_name")
        if not skill_name:
            continue
        group = grouped.setdefault(skill_name, {
            "skill_name": skill_name,
            "round_count": 0,
            "analysis_seconds": 0.0,
            "implementation_seconds": 0.0,
            "validation_seconds": 0.0,
            "total_seconds": 0.0,
            "statuses": [],
            "evidence_paths": [],
        })
        group["round_count"] += 1
        for key in ("analysis_seconds", "implementation_seconds", "validation_seconds", "total_seconds"):
            group[key] += number_value(record.get(key))
        status = record.get("status")
        if status and status not in group["statuses"]:
            group["statuses"].append(status)
        evidence_path = record.get("evidence_path")
        if evidence_path and evidence_path not in group["evidence_paths"]:
            group["evidence_paths"].append(evidence_path)

    summaries = []
    for group in grouped.values():
        group["analysis_duration"] = duration_text(group["analysis_seconds"])
        group["implementation_duration"] = duration_text(group["implementation_seconds"])
        group["validation_duration"] = duration_text(group["validation_seconds"])
        group["total_duration"] = duration_text(group["total_seconds"])
        group["statuses"] = ",".join(group["statuses"])
        group["evidence_paths"] = ",".join(group["evidence_paths"])
        summaries.append(group)
    return sorted(summaries, key=lambda item: item["skill_name"])


def kv_lines(value):
    data = as_dict(value)
    if not data:
        return "- None"
    return "\n".join(f"- {key}: {scalar(item)}" for key, item in data.items())


def bullet_lines(items, label_key=None):
    values = as_list(items)
    if not values:
        return "- None"
    lines = []
    for item in values:
        if isinstance(item, dict):
            label = item.get(label_key) if label_key else None
            if label:
                lines.append(f"- {label}: {scalar(item)}")
            else:
                lines.append(f"- {scalar(item)}")
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def table_from_dicts(items, columns):
    values = [item for item in as_list(items) if isinstance(item, dict)]
    if not values:
        return "- None"
    lines = [
        "| " + " | ".join(title for _, title in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for item in values:
        lines.append("| " + " | ".join(scalar(item.get(key, "")) for key, _ in columns) + " |")
    return "\n".join(lines)


def has_downstream_claims(data):
    for key in (
        "bottleneck_classification",
        "candidate_skill_list",
        "dynamic_route_plan",
        "candidate_pool",
        "optimization_actions",
        "selected_optimization_actions",
        "before_after_metrics",
        "improvement_summary",
        "per_skill_iteration_state",
    ):
        value = data.get(key)
        if value not in ("", None, [], {}):
            if key == "bottleneck_classification" and value in ("not_entered", "blocked", "unknown_bottleneck"):
                continue
            return True
    return False


def blocked_gate(data):
    progress = as_dict(data.get("overall_progress"))
    if progress.get("status") == "blocked" or progress.get("blocked_gate"):
        return progress.get("blocked_gate") or progress.get("current_gate") or "workflow_gate"

    if not data.get("current_run_id"):
        return "current_run_identity"

    evidence_status = data.get("current_evidence_status", "unknown")
    if evidence_status != "current":
        return "evidence_freshness_check"

    service_status = data.get("service_health_status", "unknown")
    if service_status in {"failed", "blocked"}:
        return "service_health_check"

    identity_status = as_dict(data.get("target_instance_identity")).get("status", "unknown")
    if identity_status in {"failed", "ambiguous", "blocked"}:
        return "target_instance_identity"

    baseline_status = data.get("baseline_confirmation_status", "unknown")
    if baseline_status in {"pending", "rebuild_required", "not_entered", "blocked", "failed"} and has_downstream_claims(data):
        return "baseline_confirmation_status"

    if (
        data.get("historical_records_status") == "discovered_unconfirmed"
        and data.get("historical_records_used_for_current_run") is True
    ):
        return "historical_records_confirmation"

    return ""


def force_blocked_downstream(data, gate):
    if not gate:
        return data
    blocked = dict(data)
    progress = as_dict(blocked.get("overall_progress"))
    progress.setdefault("status", "blocked")
    progress["status"] = "blocked"
    progress["blocked_gate"] = gate
    progress.setdefault("current_gate", gate)
    blocked["overall_progress"] = progress
    blocked["report_mode"] = "blocked"
    blocked["bottleneck_classification"] = "blocked"
    blocked["bottleneck_evidence"] = {
        "status": "not_entered",
        "reason": f"Blocked at {gate}; current evidence is not eligible for bottleneck classification.",
    }
    blocked["performance_signal_summary"] = {}
    blocked["candidate_skill_list"] = []
    blocked["dynamic_route_plan"] = []
    blocked["candidate_pool"] = {}
    blocked["optimization_actions"] = []
    blocked["before_after_metrics"] = []
    blocked["improvement_summary"] = {
        "status": "not_entered",
        "reason": f"Blocked at {gate}; no formal optimization gain is reported.",
    }
    blocked["per_skill_iteration_state"] = {}
    blocked["selected_optimization_actions"] = []
    return blocked


def normalize(data, input_path=None):
    normalized = dict(data)
    old_bottleneck = as_dict(data.get("bottleneck"))
    timing_jsonl_path = data.get("timing_jsonl_path", "")
    timing_records, timing_warning = load_timing_jsonl(timing_jsonl_path, input_path)

    normalized.setdefault("application_name", data.get("application", "unknown"))
    normalized.setdefault("workload_type", data.get("workload_type", "unknown"))
    normalized.setdefault("deployment_topology", data.get("deployment_topology", data.get("topology", "")))
    normalized.setdefault("overall_progress", data.get("overall_progress", {}))
    normalized.setdefault("workflow_gate_status", data.get("workflow_gate_status", data.get("gate_status", [])))
    normalized.setdefault("current_run_id", data.get("current_run_id", data.get("run_id", "")))
    normalized.setdefault("current_run_started_at", data.get("current_run_started_at", ""))
    normalized.setdefault("current_run_manifest", data.get("current_run_manifest", data.get("run_manifest_path", "")))
    normalized.setdefault("current_evidence_status", data.get("current_evidence_status", "unknown"))
    normalized.setdefault("current_evidence_paths", data.get("current_evidence_paths", []))
    normalized.setdefault("scenario_environment_summary", data.get("scenario_environment_summary", {}))
    normalized.setdefault("scenario_confirmation_status", data.get("scenario_confirmation_status", "unknown"))
    normalized.setdefault("node_inventory", data.get("node_inventory", []))
    normalized.setdefault("per_node_environment_backups", data.get("per_node_environment_backups", []))
    normalized.setdefault("per_node_environment_diagnosis", data.get("per_node_environment_diagnosis", []))
    normalized.setdefault("container_targets", data.get("container_targets", []))
    normalized.setdefault("container_execution_mode", data.get("container_execution_mode", "not_applicable"))
    normalized.setdefault("workflow_execution_plan", data.get("workflow_execution_plan", []))
    normalized.setdefault("workflow_stage_trace", data.get("workflow_stage_trace", []))
    normalized.setdefault(
        "evidence_freshness_policy",
        data.get(
            "evidence_freshness_policy",
            "Current conclusions require matching current_run_id, target identity, and collection time.",
        ),
    )
    normalized.setdefault("evidence_freshness_failure_reason", data.get("evidence_freshness_failure_reason", ""))
    normalized.setdefault("evidence_freshness_next_steps", data.get("evidence_freshness_next_steps", []))
    normalized.setdefault("service_health_status", data.get("service_health_status", "unknown"))
    normalized.setdefault("service_health_checks", data.get("service_health_checks", {}))
    normalized.setdefault("service_health_evidence", data.get("service_health_evidence", []))
    normalized.setdefault("service_health_failure_reason", data.get("service_health_failure_reason", ""))
    normalized.setdefault("service_health_next_steps", data.get("service_health_next_steps", []))
    normalized.setdefault("historical_records_status", data.get("historical_records_status", "none_found"))
    normalized.setdefault("historical_records_paths", data.get("historical_records_paths", []))
    normalized.setdefault("historical_records_summary", data.get("historical_records_summary", {}))
    normalized.setdefault("historical_records_used_for_current_run", data.get("historical_records_used_for_current_run", False))
    normalized.setdefault("historical_records_usage_scope", data.get("historical_records_usage_scope", []))
    normalized.setdefault(
        "historical_records_user_confirmation",
        data.get("historical_records_user_confirmation", "not_requested"),
    )
    normalized.setdefault(
        "historical_records_policy",
        data.get(
            "historical_records_policy",
            "Historical records must not drive tuning unless the user confirms they apply to the current target.",
        ),
    )
    normalized.setdefault("test_topology_confidence", data.get("test_topology_confidence", "unknown"))
    normalized.setdefault("test_case_confidence", data.get("test_case_confidence", "unknown"))
    normalized.setdefault("environment_snapshot", data.get("environment", {}))
    normalized.setdefault("environment_diagnosis", data.get("environment_diagnosis", {}))
    normalized.setdefault("baseline_metrics", data.get("metric_rounds", [{}])[0] if data.get("metric_rounds") else {})
    normalized.setdefault("baseline_confirmation_status", data.get("baseline_confirmation_status", "unknown"))
    normalized.setdefault("target_instance_identity", data.get("target_instance_identity", {}))
    normalized.setdefault("bottleneck_classification", old_bottleneck.get("bottleneck_type", "unknown_bottleneck"))
    normalized.setdefault("bottleneck_evidence", old_bottleneck or data.get("bottleneck_evidence", {}))
    normalized.setdefault("workflow_trace", data.get("round_decisions", []))
    candidate_skill_list = data.get("candidate_skill_list", data.get("dynamic_route_plan", []))
    normalized.setdefault("candidate_skill_list", candidate_skill_list)
    normalized.setdefault("dynamic_route_plan", candidate_skill_list)
    normalized.setdefault("performance_signal_summary_path", data.get("performance_signal_summary_path", ""))
    normalized.setdefault("performance_signal_summary", data.get("performance_signal_summary", {}))

    candidate_pool = data.get("candidate_pool", {})
    if not candidate_pool and any(key in data for key in ("candidate_pool_path", "route_summary_path", "dynamic_skill_results_dir")):
        candidate_pool = {
            "candidate_pool_path": data.get("candidate_pool_path", ""),
            "route_summary_path": data.get("route_summary_path", ""),
            "dynamic_skill_results_dir": data.get("dynamic_skill_results_dir", ""),
        }
    normalized.setdefault("candidate_pool", candidate_pool)

    normalized.setdefault("optimization_actions", data.get("selected_actions", []))
    normalized.setdefault("before_after_metrics", data.get("metric_rounds", []))
    normalized.setdefault("improvement_summary", data.get("improvement_summary", {}))
    normalized.setdefault("timing_jsonl_path", timing_jsonl_path)
    if timing_warning:
        normalized.setdefault("timing_load_warnings", []).append(timing_warning)

    optimization_timing = data.get("optimization_timing") or [
        record for record in timing_records
        if record.get("round_name") or record.get("skill_name")
    ]
    optimization_timing_details = data.get("optimization_timing_details") or optimization_timing
    agent_timing_summary = data.get("agent_timing_summary") or summarize_timing_records(timing_records or optimization_timing_details)
    per_skill_timing_summary = data.get("per_skill_timing_summary") or summarize_per_skill(optimization_timing_details)

    normalized.setdefault("agent_timing_summary", agent_timing_summary)
    normalized.setdefault("optimization_timing", optimization_timing)
    normalized.setdefault("optimization_timing_details", optimization_timing_details)
    normalized.setdefault("per_skill_timing_summary", per_skill_timing_summary)
    normalized.setdefault("per_skill_iteration_state", data.get("per_skill_iteration_state", {}))
    normalized.setdefault("selected_optimization_actions", data.get("selected_actions", []))
    normalized.setdefault("rejected_optimization_actions", data.get("deferred_actions", []))
    normalized.setdefault("review_result", data.get("review_result", {}))
    normalized.setdefault("restore_result", data.get("restore_result", {}))
    normalized.setdefault("next_steps", data.get("next_steps", []))
    normalized.setdefault("case_archive_path", data.get("case_archive_path", ""))
    normalized.setdefault("risk_and_rollback", data.get("risk_and_rollback", data.get("risks_and_rollback", [])))
    gate = blocked_gate(normalized)
    normalized.setdefault("blocked_gate", gate)
    return force_blocked_downstream(normalized, gate)


def main():
    parser = argparse.ArgumentParser(description="Generate architecture-aligned application optimization report.")
    parser.add_argument("--input", required=True, help="Report input JSON")
    parser.add_argument("--output", required=True, help="Output Markdown report")
    args = parser.parse_args()

    data = normalize(load_json(args.input), args.input)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    blocked_mode = data.get("report_mode") == "blocked"
    missing = [
        field
        for field in REQUIRED_FIELDS
        if (field not in data or data[field] in ("", None, [], {}))
        and not (blocked_mode and field in BLOCKED_ALLOWED_EMPTY_FIELDS)
    ]
    completed_mode = as_dict(data.get("overall_progress")).get("status") == "completed" and not blocked_mode
    completed_missing = [
        field
        for field in COMPLETED_HARD_REQUIRED_FIELDS
        if data.get(field) in ("", None, [], {})
    ] if completed_mode else []
    if completed_missing:
        raise SystemExit(
            "completed report is missing required workflow/timing fields: "
            + ", ".join(completed_missing)
        )
    scenario = data.get("scenario_name", "Unnamed Scenario")

    lines = [
        f"# {scenario} Optimization Report",
        "",
        "## Executive Summary",
        "",
        data.get("summary") or (
            f"Workflow is blocked at `{data.get('blocked_gate')}`; see current run status below."
            if data.get("report_mode") == "blocked"
            else "No free-form summary was provided; see structured sections below."
        ),
        "",
        "## Overall Progress",
        "",
        json_block(data.get("overall_progress", {})),
        "",
        "### Workflow Gate Status",
        "",
        table_from_dicts(
            data.get("workflow_gate_status"),
            [
                ("step", "Step"),
                ("gate", "Gate"),
                ("status", "Status"),
                ("evidence_path", "Evidence"),
                ("notes", "Notes"),
            ],
        ),
        "",
        "### Workflow Execution Plan",
        "",
        table_from_dicts(
            data.get("workflow_execution_plan"),
            [
                ("step", "Step"),
                ("phase", "Phase"),
                ("gate", "Gate"),
                ("confirmation_required", "Confirm"),
                ("expected_output", "Expected Output"),
            ],
        ),
        "",
        "### Workflow Stage Trace",
        "",
        table_from_dicts(
            data.get("workflow_stage_trace"),
            [
                ("phase", "Phase"),
                ("gate", "Gate"),
                ("status", "Status"),
                ("started_at", "Started"),
                ("ended_at", "Ended"),
                ("duration_seconds", "Seconds"),
                ("evidence_path", "Evidence"),
            ],
        ),
        "",
        "## Current Run And Evidence Freshness",
        "",
        f"- Current run ID: `{data.get('current_run_id', '')}`",
        f"- Current run started at: `{data.get('current_run_started_at', '')}`",
        f"- Current evidence status: `{data.get('current_evidence_status', 'unknown')}`",
        f"- Freshness failure reason: {data.get('evidence_freshness_failure_reason') or 'None'}",
        f"- Freshness policy: {scalar(data.get('evidence_freshness_policy'))}",
        "",
        "### Current Run Manifest",
        "",
        json_block(data.get("current_run_manifest", {})),
        "",
        "### Current Evidence Paths",
        "",
        bullet_lines(data.get("current_evidence_paths")),
        "",
        "### Evidence Freshness Next Steps",
        "",
        bullet_lines(data.get("evidence_freshness_next_steps")),
        "",
        "## Scenario And Confidence",
        "",
        f"- Application: {data.get('application_name', 'unknown')}",
        f"- Workload type: {data.get('workload_type', 'unknown')}",
        f"- Test topology confidence: {data.get('test_topology_confidence', 'unknown')}",
        f"- Test case confidence: {data.get('test_case_confidence', 'unknown')}",
        f"- Scenario confirmation: {data.get('scenario_confirmation_status', 'unknown')}",
        f"- Baseline confirmation: {data.get('baseline_confirmation_status', 'unknown')}",
        "",
        "### Scenario Environment Summary",
        "",
        json_block(data.get("scenario_environment_summary", {})),
        "",
        "### Deployment Topology",
        "",
        scalar(data.get("deployment_topology")) or "- None",
        "",
        "### Node Inventory",
        "",
        table_from_dicts(
            data.get("node_inventory"),
            [
                ("node_id", "Node"),
                ("role", "Role"),
                ("host", "Host"),
                ("user", "User"),
                ("collection_scope", "Collection Scope"),
            ],
        ),
        "",
        "### Container Targets",
        "",
        f"- Container execution mode: `{data.get('container_execution_mode', 'not_applicable')}`",
        "",
        table_from_dicts(
            data.get("container_targets"),
            [
                ("node_id", "Node"),
                ("container", "Container"),
                ("runtime", "Runtime"),
                ("entry_method", "Entry Method"),
                ("access_status", "Access"),
            ],
        ),
        "",
        "## Service Health And Target Readiness",
        "",
        f"- Service health status: `{data.get('service_health_status', 'unknown')}`",
        f"- Failure reason: {data.get('service_health_failure_reason') or 'None'}",
        "",
        "### Service Health Checks",
        "",
        json_block(data.get("service_health_checks", {})),
        "",
        "### Service Health Evidence",
        "",
        bullet_lines(data.get("service_health_evidence")),
        "",
        "### Service Health Next Steps",
        "",
        bullet_lines(data.get("service_health_next_steps")),
        "",
        "## Environment And Backup",
        "",
        kv_lines(data.get("environment_snapshot")),
        "",
        f"- Environment backup: `{data.get('environment_backup_dir', '')}`",
        "",
        "### Environment Diagnosis",
        "",
        json_block(data.get("environment_diagnosis", {})),
        "",
        "### Per-Node Environment Backups",
        "",
        json_block(data.get("per_node_environment_backups", [])),
        "",
        "### Per-Node Environment Diagnosis",
        "",
        json_block(data.get("per_node_environment_diagnosis", [])),
        "",
        "## Baseline Metrics",
        "",
        json_block(data.get("baseline_metrics", {})),
        "",
        "## Target Instance Identity",
        "",
        json_block(data.get("target_instance_identity", {})),
        "",
        "## Historical Records",
        "",
        f"- Status: `{data.get('historical_records_status', 'none_found')}`",
        f"- User confirmation: `{scalar(data.get('historical_records_user_confirmation'))}`",
        f"- Used for current run: `{scalar(data.get('historical_records_used_for_current_run'))}`",
        f"- Usage scope: `{scalar(data.get('historical_records_usage_scope'))}`",
        f"- Policy: {data.get('historical_records_policy', '')}",
        "",
        "### Historical Record Paths",
        "",
        bullet_lines(data.get("historical_records_paths")),
        "",
        "### Historical Record Summary",
        "",
        json_block(data.get("historical_records_summary", {})),
        "",
        "## Bottleneck Classification",
        "",
        f"- Classification: `{data.get('bottleneck_classification', 'unknown_bottleneck')}`",
        "",
        "### Bottleneck Evidence",
        "",
        json_block(data.get("bottleneck_evidence", {})),
        "",
        "## Workflow Trace",
        "",
        bullet_lines(data.get("workflow_trace")),
        "",
        "## Performance Signal Summary",
        "",
        f"- Summary path: `{data.get('performance_signal_summary_path', '')}`",
        "",
        json_block(data.get("performance_signal_summary", {})),
        "",
        "## Candidate Skill List",
        "",
        table_from_dicts(
            data.get("candidate_skill_list"),
            [
                ("candidate_id", "Candidate"),
                ("phase", "Phase"),
                ("subskill_name", "Skill"),
                ("priority", "Priority"),
                ("source_signal", "Signal"),
                ("reason", "Reason"),
                ("stop_rule", "Stop Rule"),
            ],
        ),
        "",
        "## Candidate Pool",
        "",
        json_block(data.get("candidate_pool", {})),
        "",
        "## Optimization Actions",
        "",
        bullet_lines(data.get("optimization_actions")),
        "",
        "## Before And After Metrics",
        "",
        table_from_dicts(
            data.get("before_after_metrics"),
            [
                ("name", "Stage"),
                ("tps", "TPS"),
                ("qps", "QPS"),
                ("avg_latency_ms", "Avg Latency ms"),
                ("p95_latency_ms", "P95 Latency ms"),
                ("max_latency_ms", "Max Latency ms"),
            ],
        ),
        "",
        "## Improvement Summary",
        "",
        json_block(data.get("improvement_summary", {})),
        "",
        "## Timing",
        "",
        f"- Timing JSONL: `{data.get('timing_jsonl_path', '')}`",
        "",
        "### Timing Load Warnings",
        "",
        bullet_lines(data.get("timing_load_warnings")),
        "",
        "### Agent Timing Summary",
        "",
        json_block(data.get("agent_timing_summary", {})),
        "",
        "### Per-Skill Timing Summary",
        "",
        table_from_dicts(
            data.get("per_skill_timing_summary"),
            [
                ("skill_name", "Skill"),
                ("round_count", "Records"),
                ("statuses", "Statuses"),
                ("analysis_duration", "Analysis"),
                ("implementation_duration", "Implementation"),
                ("validation_duration", "Validation"),
                ("total_duration", "Total"),
                ("evidence_paths", "Evidence"),
            ],
        ),
        "",
        "### Optimization Timing",
        "",
        table_from_dicts(
            data.get("optimization_timing"),
            [
                ("stage", "Stage"),
                ("skill_name", "Skill"),
                ("round_name", "Round"),
                ("status", "Status"),
                ("analysis_duration", "Analysis"),
                ("implementation_duration", "Implementation"),
                ("validation_duration", "Validation"),
                ("total_duration", "Total"),
                ("evidence_path", "Evidence"),
            ],
        ),
        "",
        "## Timing Details",
        "",
        table_from_dicts(
            data.get("optimization_timing_details"),
            [
                ("stage", "Stage"),
                ("skill_name", "Skill"),
                ("optimization_item", "Item"),
                ("round_name", "Round"),
                ("status", "Status"),
                ("analysis_duration", "Analysis"),
                ("implementation_duration", "Implementation"),
                ("validation_duration", "Validation"),
                ("total_duration", "Total"),
                ("evidence_path", "Evidence"),
            ],
        ),
        "",
        "## Per-Skill Iteration State",
        "",
        json_block(data.get("per_skill_iteration_state", {})),
        "",
        "## Selected Actions",
        "",
        bullet_lines(data.get("selected_optimization_actions")),
        "",
        "## Rejected Or Deferred Actions",
        "",
        bullet_lines(data.get("rejected_optimization_actions")),
        "",
        "## Risks And Rollback",
        "",
        bullet_lines(data.get("risk_and_rollback")),
        "",
        "## Review Result",
        "",
        json_block(data.get("review_result", {})),
        "",
        "## Restore Result",
        "",
        json_block(data.get("restore_result", {})),
        "",
        "## Case Archive",
        "",
        f"- Archive path: `{data.get('case_archive_path', '')}`",
        "",
        "## Next Steps",
        "",
        bullet_lines(data.get("next_steps")),
    ]

    if missing:
        lines.extend([
            "",
            "## Input Gaps",
            "",
            "The report was generated with missing architecture fields. Fill these before final delivery:",
            "",
            bullet_lines(missing),
        ])

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated report at {output}")
    if missing:
        print("Missing architecture fields: " + ", ".join(missing))


if __name__ == "__main__":
    main()
