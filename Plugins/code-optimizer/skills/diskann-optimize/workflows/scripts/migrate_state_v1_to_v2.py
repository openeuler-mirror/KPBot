#!/usr/bin/env python3
"""Preview or migrate a legacy vector-opt-run.v1 state to local-first, optional-CP8 v2."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any


EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


def read_state(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("state root must be an object")
    if payload.get("schema_version") != "vector-opt-run.v1":
        raise ValueError("input must use vector-opt-run.v1")
    stages = payload.get("stages")
    if not isinstance(stages, dict) or set(stages) != {str(index) for index in range(8)}:
        raise ValueError("legacy state must contain exactly stages 0..7")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--wiki-root", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--experiment-id")
    parser.add_argument("--check", action="store_true", help="preview migration without writing")
    parser.add_argument("--apply", action="store_true", help="create backup and atomically replace state")
    args = parser.parse_args()
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")

    try:
        state_path = args.state.resolve()
        payload = read_state(state_path)
        workflow = payload.get("workflow")
        needs_sync = workflow == "diskann-optimize" and args.wiki_root is not None
        experiment_id = args.experiment_id or str(payload.get("run_id", ""))
        if needs_sync:
            if args.wiki_root is None or args.experiment_root is None:
                parser.error("optimization state migration requires --wiki-root and --experiment-root")
            if not EXPERIMENT_ID.fullmatch(experiment_id):
                parser.error("invalid --experiment-id and legacy run_id cannot be used")
            wiki_root = args.wiki_root.resolve()
            experiment_root = args.experiment_root.resolve()
            if not wiki_root.is_dir() or not (wiki_root / "AGENTS.md").is_file():
                parser.error("--wiki-root must contain AGENTS.md")
            expected_run_dir = experiment_root / experiment_id
            if state_path.parent != expected_run_dir:
                raise ValueError(
                    "legacy state must already be located at <experiment-root>/<experiment-id>; "
                    "move the complete run directory before migration"
                )
        else:
            wiki_root = None
            experiment_root = state_path.parent.parent
            experiment_id = state_path.parent.name

        if workflow not in {"diskann-optimize", "diskann-source-investigation"}:
            raise ValueError("unsupported DiskANN workflow")
        migrated = dict(payload)
        source = migrated.get("source")
        if isinstance(source, dict) and source.get("baseline_commit") is None:
            source["baseline_commit"] = "unknown"
        migrated["schema_version"] = "vector-opt-run.v2"
        migrated["experiment"] = {
            "experiment_root": str(experiment_root) if experiment_root else None,
            "experiment_id": experiment_id,
            "run_dir": str(state_path.parent),
            "wiki_root": str(wiki_root) if wiki_root else None,
        }
        migrated_stages = dict(payload["stages"])
        migrated_stages["8"] = {
            "status": "pending" if needs_sync else "skipped",
            "artifacts": [],
            "verdict": None if needs_sync else "NOT_APPLICABLE",
        }
        migrated["stages"] = migrated_stages
        migrated["knowledge_sync"] = {
            "status": "pending" if needs_sync else "not_applicable",
            "raw_path": None,
            "experiment_page": None,
            "decision_page": None,
            "receipt": None,
            "validation_receipt": None,
        }

        result = {
            "status": "PREVIEW" if args.check else "MIGRATED",
            "state": str(state_path),
            "schema_version": "vector-opt-run.v2",
            "cp8": migrated_stages["8"]["status"],
            "experiment_id": experiment_id,
        }
        from validate_state import validate
        validate(migrated, state_path)
        if args.apply:
            backup = state_path.with_name(state_path.name + ".v1.bak")
            if backup.exists():
                raise ValueError(f"backup already exists; refusing to overwrite: {backup}")
            shutil.copy2(state_path, backup)
            temporary = state_path.with_name(state_path.name + ".v2.tmp")
            temporary.write_text(
                json.dumps(migrated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, state_path)
            result["backup"] = str(backup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"MIGRATION_BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
