#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


REQUIRED_RESULT_FIELDS = {
    "subskill_name",
    "current_run_id",
    "current_evidence_status",
    "status",
    "analysis_timestamp",
    "evidence_sources",
    "findings",
    "candidate_actions",
    "required_evidence",
    "confidence",
    "fallback_notes",
    "timing",
    "subagent_id",
    "result_path",
}

REQUIRED_ACTION_FIELDS = {
    "action_id",
    "title",
    "category",
    "priority",
    "change_mode",
    "requires_root",
    "risk",
    "implementation_plan",
    "validation_plan",
    "rollback",
    "expected_effect",
    "expected_gain_metric",
    "rejection_criteria",
    "evidence_refs",
}

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def load_result(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [f"invalid_json:{path.name}:{exc}"]

    if "candidate_actions" not in data and isinstance(data.get("selected_optimization_actions"), list):
        data["candidate_actions"] = data["selected_optimization_actions"]

    missing = sorted(REQUIRED_RESULT_FIELDS - set(data))
    errors = [f"missing_result_fields:{path.name}:{','.join(missing)}"] if missing else []

    if data.get("status") not in {"ok", "degraded", "blocked", "failed"}:
        errors.append(f"invalid_status:{path.name}:{data.get('status')}")

    if not isinstance(data.get("candidate_actions", []), list):
        errors.append(f"candidate_actions_not_list:{path.name}")

    for index, action in enumerate(data.get("candidate_actions", [])):
        if not isinstance(action, dict):
            errors.append(f"candidate_action_not_object:{path.name}:{index}")
            continue
        missing_action = sorted(REQUIRED_ACTION_FIELDS - set(action))
        if missing_action:
            errors.append(f"missing_action_fields:{path.name}:{index}:{','.join(missing_action)}")

    return data, errors


def sort_key(action):
    return (
        PRIORITY_ORDER.get(str(action.get("priority", "")).lower(), 9),
        RISK_ORDER.get(str(action.get("risk", "")).lower(), 9),
        str(action.get("category", "")),
        str(action.get("action_id", "")),
    )


def main():
    parser = argparse.ArgumentParser(description="Merge candidate skill analysis JSON results into a candidate pool.")
    parser.add_argument("--results-dir", required=True, help="Directory containing subagent result JSON files")
    parser.add_argument("--output-candidate-pool", default="output/candidate_pool.json", help="Output candidate pool JSON")
    parser.add_argument("--output-summary", default="output/candidate-skill-summary.md", help="Output Markdown summary")
    parser.add_argument("--gate-check", action="store_true", help="Fail if expected candidate subskills are missing, blocked, failed, or have no candidates")
    parser.add_argument("--expected-subskills", default="", help="Comma-separated subskills expected in this candidate skill list")
    parser.add_argument("--optimization-order", default="", help="Comma-separated optimization order to write to candidate_pool.json")
    parser.add_argument("--candidate-manifest", default="", help="Task manifest JSON containing candidate_skill_list")
    parser.add_argument("--route-manifest", default="", help="Deprecated alias for --candidate-manifest")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        raise SystemExit(f"results directory does not exist: {results_dir}")

    subagent_results = []
    validation_errors = []
    degraded = []
    actions_by_id = {}
    seen_subskills = set()
    gate_errors = []

    for path in sorted(results_dir.glob("*.json")):
        data, errors = load_result(path)
        validation_errors.extend(errors)
        if data is None:
            degraded.append({"file": path.name, "reason": "invalid_json"})
            continue

        subagent_results.append(data)
        seen_subskills.add(data.get("subskill_name", path.stem))
        if data.get("status") in {"degraded", "blocked", "failed"}:
            degraded.append({
                "subskill_name": data.get("subskill_name", path.stem),
                "status": data.get("status"),
                "fallback_notes": data.get("fallback_notes", []),
                "required_evidence": data.get("required_evidence", []),
            })

        if errors:
            degraded.append({
                "subskill_name": data.get("subskill_name", path.stem),
                "status": "degraded",
                "fallback_notes": errors,
            })

        for action in data.get("candidate_actions", []):
            if not isinstance(action, dict) or "action_id" not in action:
                continue
            action = dict(action)
            action.setdefault("source_subskill", data.get("subskill_name", path.stem))
            existing = actions_by_id.get(action["action_id"])
            if existing is None or sort_key(action) < sort_key(existing):
                actions_by_id[action["action_id"]] = action

    candidate_actions = sorted(actions_by_id.values(), key=sort_key)

    candidate_skill_list = []
    bottleneck_classification = ""
    candidate_manifest = {}
    manifest_arg = args.candidate_manifest or args.route_manifest
    if manifest_arg:
        manifest_path = Path(manifest_arg)
        try:
            candidate_manifest = json.loads(manifest_path.read_text())
            bottleneck_classification = candidate_manifest.get("bottleneck_classification", "")
            candidate_skill_list = candidate_manifest.get("candidate_skill_list", candidate_manifest.get("dynamic_route_plan", []))
        except Exception as exc:
            gate_errors.append(f"invalid_candidate_manifest:{manifest_path}:{exc}")

    expected_subskills = [item.strip() for item in args.expected_subskills.split(",") if item.strip()]
    if not expected_subskills:
        expected_subskills = [
            str(candidate.get("subskill_name", "")).strip()
            for candidate in candidate_skill_list
            if str(candidate.get("subskill_name", "")).strip()
        ]
    if not expected_subskills:
        expected_subskills = sorted(seen_subskills)

    missing_subskills = sorted(set(expected_subskills) - seen_subskills)
    if missing_subskills:
        gate_errors.append(f"missing_subskills:{','.join(missing_subskills)}")

    bad_status = sorted(
        str(item.get("subskill_name", item.get("file", "unknown")))
        for item in degraded
        if item.get("status") in {"blocked", "failed"} or item.get("reason") == "invalid_json"
    )
    if bad_status:
        gate_errors.append(f"blocked_or_failed_subskills:{','.join(bad_status)}")

    if not candidate_actions:
        gate_errors.append("candidate_actions_empty")

    if validation_errors:
        gate_errors.append("validation_errors_present")

    optimization_order = [item.strip() for item in args.optimization_order.split(",") if item.strip()]
    if not optimization_order:
        optimization_order = expected_subskills

    current_run_id = candidate_manifest.get("current_run_id", "")
    current_evidence_status = candidate_manifest.get("current_evidence_status", "")
    current_run_started_at = candidate_manifest.get("current_run_started_at", "")
    current_run_manifest = candidate_manifest.get("current_run_manifest", {})
    evidence_metadata_path = candidate_manifest.get("evidence_metadata_path", "")
    performance_signal_summary_path = candidate_manifest.get("performance_signal_summary_path", "")

    if not current_run_id:
        gate_errors.append("missing_current_run_id_in_candidate_manifest")
    if current_evidence_status != "current":
        gate_errors.append(f"candidate_manifest_current_evidence_status_not_current:{current_evidence_status or 'missing'}")

    for result in subagent_results:
        result_run_id = result.get("current_run_id", "")
        result_evidence_status = result.get("current_evidence_status", "")
        if result_run_id != current_run_id:
            gate_errors.append(
                f"current_run_id_mismatch:{result.get('subskill_name', 'unknown')}:{result_run_id}"
            )
        if result_evidence_status != current_evidence_status:
            gate_errors.append(
                f"current_evidence_status_mismatch:{result.get('subskill_name', 'unknown')}:{result_evidence_status}"
            )

    for action in candidate_actions:
        action.setdefault("current_run_id", current_run_id)
        action.setdefault("current_evidence_status", current_evidence_status)

    candidate_pool = {
        "schema_version": "1.0",
        "current_run_id": current_run_id,
        "current_run_started_at": current_run_started_at,
        "current_run_manifest": current_run_manifest,
        "current_evidence_status": current_evidence_status,
        "evidence_metadata_path": evidence_metadata_path,
        "performance_signal_summary_path": performance_signal_summary_path,
        "bottleneck_classification": bottleneck_classification,
        "candidate_actions": candidate_actions,
        "optimization_order": optimization_order,
        "candidate_skill_list": candidate_skill_list,
        "dynamic_route_plan": candidate_skill_list,
        "subagent_results": subagent_results,
        "validation_errors": validation_errors,
        "gate_errors": gate_errors,
        "degraded_capabilities": degraded,
        "summary": {
            "subagent_result_count": len(subagent_results),
            "candidate_action_count": len(candidate_actions),
            "degraded_count": len(degraded),
            "gate_error_count": len(gate_errors),
        },
    }

    output_pool = Path(args.output_candidate_pool)
    output_pool.parent.mkdir(parents=True, exist_ok=True)
    output_pool.write_text(json.dumps(candidate_pool, indent=2, ensure_ascii=False) + "\n")

    output_summary = Path(args.output_summary)
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Candidate Skill Analysis Summary",
        "",
        f"- Subagent results: {len(subagent_results)}",
        f"- Candidate actions: {len(candidate_actions)}",
        f"- Degraded capabilities: {len(degraded)}",
        f"- Candidate skills: {len(candidate_skill_list)}",
        "",
        "## Candidate Skill List",
        "",
    ]
    if candidate_skill_list:
        for candidate in candidate_skill_list:
            lines.append(
                f"- `{candidate.get('subskill_name')}` [{candidate.get('phase')}/{candidate.get('priority')}] "
                f"{candidate.get('source_signal')} - {candidate.get('reason')}"
            )
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Candidate Actions",
        "",
    ])
    if candidate_actions:
        for action in candidate_actions:
            lines.append(
                f"- `{action.get('action_id')}` [{action.get('priority')}/{action.get('risk')}] "
                f"{action.get('category')} - {action.get('expected_effect')}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "## Degraded Capabilities", ""])
    if degraded:
        for item in degraded:
            label = item.get("subskill_name") or item.get("file")
            lines.append(f"- `{label}`: {item.get('status', 'degraded')}")
    else:
        lines.append("- None")

    if validation_errors:
        lines.extend(["", "## Validation Errors", ""])
        for error in validation_errors:
            lines.append(f"- {error}")

    if gate_errors:
        lines.extend(["", "## Gate Errors", ""])
        for error in gate_errors:
            lines.append(f"- {error}")

    output_summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(candidate_pool["summary"], indent=2, ensure_ascii=False))
    if args.gate_check and gate_errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
