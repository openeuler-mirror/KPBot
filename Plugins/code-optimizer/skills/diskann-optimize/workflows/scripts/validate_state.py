#!/usr/bin/env python3
"""Validate DiskANN state structure and local artifact/phase invariants."""

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath


STATUSES = {"pending", "running", "completed", "blocked", "failed", "skipped"}
TERMINAL = {"ACCEPT", "ACCEPT_WITH_NOTES", "REJECT", "INCONCLUSIVE"}
SYNC = {"pending", "running", "completed", "blocked", "failed", "not_applicable"}
ID = r"^[a-z0-9][a-z0-9._-]{2,127}$"
COMMIT = r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$"


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def relative_artifact(value, run_dir, must_exist=False):
    if not nonempty(value):
        raise ValueError("artifact must be a non-empty relative path")
    win, posix = PureWindowsPath(value), PurePosixPath(value)
    if win.drive or win.root or posix.is_absolute() or ".." in win.parts or ".." in posix.parts:
        raise ValueError(f"artifact escapes run_dir: {value}")
    path = (run_dir / value.replace("\\", "/")).resolve()
    if not path.is_relative_to(run_dir.resolve()):
        raise ValueError(f"artifact escapes run_dir: {value}")
    if must_exist and not path.exists():
        raise ValueError(f"artifact does not exist: {value}")
    return path


def validate(payload, state_path):
    if not isinstance(payload, dict):
        raise ValueError("root must be an object")
    if payload.get("schema_version") != "vector-opt-run.v2":
        raise ValueError("schema_version must be vector-opt-run.v2")
    workflow = payload.get("workflow")
    if not isinstance(workflow, str) or workflow not in {
        "diskann-optimize", "diskann-source-investigation"
    }:
        raise ValueError("unsupported workflow")
    for name in ("run_id", "repo"):
        if not nonempty(payload.get(name)):
            raise ValueError(f"{name} must be a non-empty string")
    if not re.fullmatch(ID, payload["run_id"]):
        raise ValueError("invalid run_id")
    shapes = {"source": dict, "contract": dict, "experiment": dict, "stages": dict,
              "results": dict, "decisions": list, "knowledge_sync": dict}
    for name, expected in shapes.items():
        if not isinstance(payload.get(name), expected):
            raise ValueError(f"{name} has invalid type")
    if not all(isinstance(x, dict) for x in payload["decisions"]):
        raise ValueError("decisions must contain objects")
    if "selected_candidate" not in payload or (
        payload["selected_candidate"] is not None and not nonempty(payload["selected_candidate"])
    ):
        raise ValueError("selected_candidate must be null or non-empty string")
    source = payload["source"]
    for name in ("baseline_commit", "candidate_commit"):
        value = source.get(name)
        allowed_unknown = name == "baseline_commit" and value == "unknown"
        if name not in source or not (allowed_unknown or (name == "candidate_commit" and value is None)
                                      or (isinstance(value, str) and re.fullmatch(COMMIT, value))):
            raise ValueError(f"source.{name} must be a full SHA or allowed unknown/null")
    contract = payload["contract"]
    if type(contract.get("frozen")) is not bool or "manifest" not in contract:
        raise ValueError("contract requires boolean frozen and manifest")
    run_dir = state_path.resolve().parent
    if contract["manifest"] is not None:
        relative_artifact(contract["manifest"], run_dir, contract["frozen"])
    if contract["frozen"] and not nonempty(contract["manifest"]):
        raise ValueError("frozen contract requires manifest")
    stages = payload["stages"]
    if set(stages) != {str(i) for i in range(9)}:
        raise ValueError("stages must contain exactly keys 0..8")
    # Validate all shapes before traversing previous stages.
    if not all(isinstance(s, dict) for s in stages.values()):
        raise ValueError("each stage must be an object")
    running = 0
    for name, stage in stages.items():
        status = stage.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f"stage {name} invalid status")
        if workflow == "diskann-optimize" and name != "8" and status == "skipped":
            raise ValueError(f"optimization stage {name} cannot be skipped")
        running += status == "running"
        if "verdict" not in stage or (stage["verdict"] is not None and not nonempty(stage["verdict"])):
            raise ValueError(f"stage {name} invalid verdict")
        artifacts = stage.get("artifacts")
        if not isinstance(artifacts, list) or (status == "completed" and not artifacts):
            raise ValueError(f"stage {name} requires artifacts")
        for artifact in artifacts:
            relative_artifact(artifact, run_dir, status == "completed")
        if status in {"running", "completed"}:
            for previous in range(int(name)):
                if stages[str(previous)].get("status") not in {"completed", "skipped"}:
                    raise ValueError(f"stage {name} precedes unfinished stage {previous}")
    if running > 1:
        raise ValueError("at most one stage may be running")
    if stages["2"]["status"] == "completed" and workflow == "diskann-optimize" and not contract["frozen"]:
        raise ValueError("completed CP2 requires frozen manifest")
    experiment, sync = payload["experiment"], payload["knowledge_sync"]
    expected_keys = {"experiment_root", "experiment_id", "run_dir", "wiki_root"}
    if set(experiment) != expected_keys:
        raise ValueError("experiment fields mismatch")
    for name in expected_keys - {"wiki_root"}:
        if not nonempty(experiment[name]):
            raise ValueError(f"experiment.{name} is required")
    if not re.fullmatch(ID, experiment["experiment_id"]):
        raise ValueError("invalid experiment ID")
    if Path(experiment["run_dir"]).resolve() != run_dir or (
        Path(experiment["experiment_root"]).resolve() / experiment["experiment_id"] != run_dir
    ):
        raise ValueError("run_dir must equal <experiment_root>/<experiment_id> and state parent")
    sync_keys = {"status", "raw_path", "experiment_page", "decision_page", "receipt", "validation_receipt"}
    if set(sync) != sync_keys or not isinstance(sync["status"], str) or sync["status"] not in SYNC:
        raise ValueError("knowledge_sync fields/status invalid")
    for name in sync_keys - {"status"}:
        if sync[name] is not None and not nonempty(sync[name]):
            raise ValueError(f"knowledge_sync.{name} invalid")
    wiki = experiment["wiki_root"]
    if wiki is not None and not nonempty(wiki):
        raise ValueError("wiki_root must be null or non-empty string")
    enabled = wiki is not None and workflow == "diskann-optimize"
    if not enabled:
        if wiki is not None or sync["status"] != "not_applicable" or stages["8"]["status"] != "skipped":
            raise ValueError("disabled CP8 must be skipped/not_applicable without wiki_root")
    elif sync["status"] == "not_applicable":
        raise ValueError("enabled CP8 requires sync status")
    if workflow == "diskann-source-investigation":
        if any(stages[str(i)]["status"] != "skipped" for i in (6, 7, 8)):
            raise ValueError("investigation skips CP6..CP8")
    elif stages["7"]["status"] == "completed":
        verdict = stages["7"]["verdict"]
        if verdict not in TERMINAL or payload["results"].get("verdict") != verdict:
            raise ValueError("CP7 requires matching terminal stage/results verdict")
        if verdict in {"ACCEPT", "ACCEPT_WITH_NOTES"} and payload["results"].get("correctness") is not True:
            raise ValueError("acceptance requires results.correctness=true")
    if stages["8"]["status"] in {"running", "completed"}:
        if stages["7"]["status"] != "completed" or stages["7"]["verdict"] not in TERMINAL:
            raise ValueError("CP8 requires terminal CP7")
        if sync["status"] != stages["8"]["status"]:
            raise ValueError("CP8 and knowledge_sync status mismatch")
    if stages["8"]["status"] == "completed":
        for name in sync_keys - {"status"}:
            if not nonempty(sync[name]):
                raise ValueError(f"completed CP8 requires {name}")
        for name in ("receipt", "validation_receipt"):
            relative_artifact(sync[name], run_dir, True)
    elif sync["status"] == "completed":
        raise ValueError("knowledge_sync cannot complete before CP8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        validate(json.loads(args.state.read_text(encoding="utf-8")), args.state)
    except (OSError, ValueError, TypeError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
