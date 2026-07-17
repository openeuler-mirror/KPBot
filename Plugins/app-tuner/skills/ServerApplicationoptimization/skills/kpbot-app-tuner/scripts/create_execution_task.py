#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_optional_json(path):
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    return json.loads(file_path.read_text(encoding="utf-8"))


def load_evidence_metadata(evidence_dir):
    if not evidence_dir:
        return {}
    metadata_path = Path(evidence_dir) / "snapshot_metadata.json"
    if not metadata_path.exists():
        raise SystemExit(f"missing evidence metadata: {metadata_path}")
    return load_json(metadata_path)


def should_stop_skill(per_skill_state, subskill_name):
    state = per_skill_state.get(subskill_name, {}) if isinstance(per_skill_state, dict) else {}
    rounds_attempted = int(state.get("rounds_attempted", state.get("round_count", 0)) or 0)
    if rounds_attempted >= 5:
        return True, f"{subskill_name} already attempted {rounds_attempted} rounds"
    gains = state.get("round_gains_pct", [])
    if isinstance(gains, list) and len(gains) >= 5:
        last_five = gains[-5:]
        try:
            if all(float(value) < 1.0 for value in last_five):
                return True, f"{subskill_name} last 5 gains are all < 1%"
        except (TypeError, ValueError):
            return False, ""
    return False, ""


def parse_iso_timestamp(value, label):
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(f"invalid {label} timestamp: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Create one serial execution-validation task for a routed skill round.")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--round", required=True, dest="round_name")
    parser.add_argument("--subskill", required=True)
    parser.add_argument("--candidate-pool", required=True)
    parser.add_argument("--current-run-id", required=True, help="Current run id that candidate pool and evidence must match")
    parser.add_argument("--current-run-manifest", default="", help="Path to current run manifest JSON")
    parser.add_argument("--per-skill-state", default="", help="Per-skill iteration state JSON; used to enforce max 5 rounds")
    parser.add_argument("--action-id", action="append", required=True, help="Candidate action id; may be repeated")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--previous-round", default="")
    parser.add_argument("--evidence-dir", default="")
    parser.add_argument("--output-dir", default="output/execution-tasks")
    parser.add_argument("--result-path", default="")
    parser.add_argument("--agent-action-mode", default="analysis_only")
    parser.add_argument("--execution-authorization-scope", default="", help="Path to confirmed execution authorization scope JSON")
    args = parser.parse_args()

    candidate_pool = load_json(args.candidate_pool)
    pool_run_id = candidate_pool.get("current_run_id", "")
    if not pool_run_id:
        raise SystemExit("candidate_pool is missing current_run_id; refuse to create execution task")
    if pool_run_id != args.current_run_id:
        raise SystemExit(
            f"candidate_pool current_run_id mismatch: pool={pool_run_id!r}, requested={args.current_run_id!r}"
        )
    pool_evidence_status = candidate_pool.get("current_evidence_status", "")
    if pool_evidence_status != "current":
        raise SystemExit(f"candidate_pool current_evidence_status must be 'current', got: {pool_evidence_status!r}")
    gate_errors = candidate_pool.get("gate_errors", [])
    if gate_errors:
        raise SystemExit("candidate_pool has unresolved gate_errors; refuse to create execution task: " + ",".join(gate_errors))

    evidence_metadata = load_evidence_metadata(args.evidence_dir)
    evidence_run_id = evidence_metadata.get("current_run_id", "")
    evidence_status = evidence_metadata.get("current_evidence_status", "")
    if evidence_run_id and evidence_run_id != args.current_run_id:
        raise SystemExit(
            f"evidence current_run_id mismatch: metadata={evidence_run_id!r}, requested={args.current_run_id!r}"
        )
    if evidence_status and evidence_status != "current":
        raise SystemExit(f"evidence current_evidence_status must be 'current', got: {evidence_status!r}")

    current_run_started_at = candidate_pool.get("current_run_started_at", evidence_metadata.get("current_run_started_at", ""))
    snapshot_time = evidence_metadata.get("snapshot_time", "")
    started_at_dt = parse_iso_timestamp(current_run_started_at, "current_run_started_at")
    snapshot_time_dt = parse_iso_timestamp(snapshot_time, "snapshot_time")
    if started_at_dt and snapshot_time_dt and snapshot_time_dt < started_at_dt:
        raise SystemExit(
            "evidence snapshot_time is earlier than current_run_started_at; "
            f"snapshot_time={snapshot_time!r}, current_run_started_at={current_run_started_at!r}"
        )

    current_run_manifest = load_optional_json(args.current_run_manifest)
    execution_authorization_scope = load_optional_json(args.execution_authorization_scope)
    per_skill_state = load_optional_json(args.per_skill_state)
    stop, reason = should_stop_skill(per_skill_state, args.subskill)
    if stop:
        raise SystemExit(f"skill iteration stop gate: {reason}; refuse to create another execution task")

    actions = candidate_pool.get("candidate_actions", [])
    requested = set(args.action_id)
    selected = [
        action for action in actions
        if action.get("action_id") in requested and action.get("source_subskill", args.subskill) == args.subskill
    ]
    missing = sorted(requested - {action.get("action_id") for action in selected})
    if missing:
        raise SystemExit("missing candidate actions for subskill: " + ",".join(missing))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.result_path or str(output_dir.parent / "rounds" / f"{args.round_name}_summary.json")

    task = {
        "schema_version": "1.0",
        "task_type": "execution_validation",
        "scenario_name": args.scenario,
        "current_run_id": args.current_run_id,
        "current_run_started_at": current_run_started_at,
        "current_run_manifest": current_run_manifest or candidate_pool.get("current_run_manifest", {}),
        "current_evidence_status": pool_evidence_status,
        "evidence_metadata_path": str(Path(args.evidence_dir) / "snapshot_metadata.json") if args.evidence_dir else "",
        "round": args.round_name,
        "subskill_name": args.subskill,
        "agent_action_mode": args.agent_action_mode,
        "baseline_path": args.baseline,
        "previous_round_summary_path": args.previous_round,
        "candidate_pool_path": args.candidate_pool,
        "per_skill_state_path": args.per_skill_state,
        "evidence_snapshot_dir": args.evidence_dir,
        "execution_authorization_scope": execution_authorization_scope,
        "scope_change_confirmation_required": False,
        "selected_actions": selected,
        "required_output_path": result_path,
        "instructions": [
            "This task must be executed in an independent execution-validation subagent context; the main agent must not perform the environment change directly.",
            "Execute only this round and only these selected actions.",
            "Only execute if current_run_id matches candidate_pool, evidence metadata, and target identity evidence.",
            "Before changing anything, verify agent_action_mode, target identity, resource constraints, validation command, and rollback plan.",
            "Do not ask the user for per-skill or per-round approval; validate against execution_authorization_scope instead.",
            "If approval scope or evidence is insufficient, output blocked_scope_change_required or blocked and do not change the environment.",
            "After implementation, run validation, calculate stage and cumulative gain, and write the round summary.",
            "Rollback immediately if identity checks fail, validation regresses beyond rejection criteria, or the action violates the approved scope.",
            "Return subagent_id and timing with analysis_seconds, implementation_seconds, validation_seconds, and total_seconds.",
        ],
    }

    task_path = output_dir / f"{args.round_name}_{args.subskill}.json"
    task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"execution_task_path": str(task_path), "result_path": result_path}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
