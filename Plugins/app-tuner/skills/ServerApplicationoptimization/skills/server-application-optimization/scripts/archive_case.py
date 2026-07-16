#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def load_json(path, default):
    if not path:
        return default
    file_path = Path(path)
    if not file_path.exists():
        return default
    return json.loads(file_path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Create an application optimization case archive.")
    parser.add_argument("--scenario-name", required=True)
    parser.add_argument("--workload-type", default="unknown")
    parser.add_argument("--deployment-topology", default="")
    parser.add_argument("--target-resource-profile", default="")
    parser.add_argument("--environment-backup-dir", default="")
    parser.add_argument("--environment-diagnosis", default="", help="Environment diagnosis JSON")
    parser.add_argument("--baseline", default="", help="Baseline metrics JSON")
    parser.add_argument("--progress", default="", help="Overall progress and workflow gate status JSON")
    parser.add_argument("--current-run", default="", help="Current run identity and evidence freshness JSON")
    parser.add_argument("--service-health", default="", help="Service health check JSON")
    parser.add_argument("--historical-records", default="", help="Historical records status JSON")
    parser.add_argument("--candidate-pool", default="", help="Candidate pool JSON")
    parser.add_argument("--review-result", default="", help="Review result JSON")
    parser.add_argument("--restore-result", default="", help="Restore result JSON")
    parser.add_argument("--final-report", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--reuse-tag", action="append", default=[])
    parser.add_argument("--lesson", action="append", default=[])
    args = parser.parse_args()

    baseline = load_json(args.baseline, {})
    progress = load_json(args.progress, {})
    current_run = load_json(args.current_run, {})
    service_health = load_json(args.service_health, {})
    historical_records = load_json(args.historical_records, {})
    environment_diagnosis = load_json(args.environment_diagnosis, {})
    candidate_pool = load_json(args.candidate_pool, {})
    review_result = load_json(args.review_result, {})
    restore_result = load_json(args.restore_result, {})

    archive = {
        "schema_version": "1.0",
        "scenario_name": args.scenario_name,
        "workload_type": args.workload_type,
        "deployment_topology": args.deployment_topology,
        "target_resource_profile": args.target_resource_profile,
        "overall_progress": progress.get("overall_progress", progress),
        "workflow_gate_status": progress.get("workflow_gate_status", []),
        "workflow_execution_plan": progress.get("workflow_execution_plan", []),
        "workflow_stage_trace": progress.get("workflow_stage_trace", []),
        "current_run_id": current_run.get("current_run_id", current_run.get("run_id", "")),
        "current_run_started_at": current_run.get("current_run_started_at", ""),
        "current_run_manifest": current_run.get("current_run_manifest", current_run.get("run_manifest", {})),
        "current_evidence_status": current_run.get("current_evidence_status", ""),
        "current_evidence_paths": current_run.get("current_evidence_paths", []),
        "service_health_status": service_health.get("service_health_status", ""),
        "service_health_checks": service_health.get("service_health_checks", service_health),
        "service_health_evidence": service_health.get("service_health_evidence", []),
        "historical_records_status": historical_records.get("historical_records_status", ""),
        "historical_records_user_confirmation": historical_records.get("historical_records_user_confirmation", ""),
        "environment_backup_dir": args.environment_backup_dir,
        "environment_diagnosis": environment_diagnosis,
        "scenario_environment_summary": candidate_pool.get("scenario_environment_summary", {}),
        "node_inventory": candidate_pool.get("node_inventory", []),
        "per_node_environment_diagnosis": environment_diagnosis.get("per_node_environment_diagnosis", []),
        "baseline_metrics": baseline,
        "bottleneck_classification": candidate_pool.get("bottleneck_classification", ""),
        "performance_signal_summary_path": candidate_pool.get("performance_signal_summary_path", ""),
        "candidate_skill_list": candidate_pool.get("candidate_skill_list", candidate_pool.get("dynamic_route_plan", [])),
        "dynamic_route_plan": candidate_pool.get("dynamic_route_plan", candidate_pool.get("candidate_skill_list", [])),
        "accepted_actions": review_result.get("accepted_actions", []),
        "rejected_actions": review_result.get("rejected_actions", []),
        "per_skill_iteration_state": review_result.get("per_skill_iteration_state", {}),
        "improvement_summary": review_result.get("improvement_summary", {}),
        "agent_timing_summary": review_result.get("agent_timing_summary", {}),
        "optimization_timing": review_result.get("optimization_timing", []),
        "optimization_timing_details": review_result.get("optimization_timing_details", []),
        "final_report_path": args.final_report,
        "review_result": review_result,
        "restore_result": restore_result,
        "reuse_tags": args.reuse_tag,
        "lessons_learned": args.lesson,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(archive, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"case_archive_path": str(output)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
