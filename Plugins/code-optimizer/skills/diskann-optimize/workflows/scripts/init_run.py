#!/usr/bin/env python3
"""Initialize a local DiskANN experiment; knowledge export is opt-in."""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


def git_commit(repo):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True, encoding="utf-8", errors="replace", check=False,
        )
        # An unpacked source under another workspace is not that workspace's HEAD.
        if result.returncode or Path(result.stdout.strip()).resolve() != repo.resolve():
            return "unknown"
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
            encoding="utf-8", errors="replace", check=False,
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(
            r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value
        ) else "unknown"
    except OSError:
        return "unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True, choices=(
        "diskann-optimize", "diskann-source-investigation"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--experiment-id")
    parser.add_argument("--wiki-root", type=Path, help="optional requested knowledge-export target")
    args = parser.parse_args()
    if not EXPERIMENT_ID.fullmatch(args.run_id):
        parser.error("invalid run-id (3-128 lowercase letters, digits, dot, underscore, hyphen)")
    repo = args.repo.resolve()
    if not repo.is_dir():
        parser.error("source directory does not exist")
    if (args.run_dir is None) == (args.experiment_root is None):
        parser.error("choose exactly one of --run-dir and --experiment-root")
    experiment_id = args.experiment_id or args.run_id
    if not EXPERIMENT_ID.fullmatch(experiment_id):
        parser.error("invalid experiment ID")
    investigation = args.workflow == "diskann-source-investigation"
    if investigation and args.wiki_root is not None:
        parser.error("source investigation does not enable knowledge export")
    run_dir = (args.run_dir or args.experiment_root / experiment_id).resolve()
    root = args.experiment_root.resolve() if args.experiment_root else run_dir.parent
    if args.run_dir and args.experiment_id and args.experiment_id != run_dir.name:
        parser.error("experiment-id must equal the run directory name")
    experiment_id = run_dir.name
    if not EXPERIMENT_ID.fullmatch(experiment_id):
        parser.error("run directory name must be a valid experiment ID")
    enabled = args.wiki_root is not None
    now = datetime.now(timezone.utc).isoformat()
    stages = {str(i): {"status": "pending", "artifacts": [], "verdict": None}
              for i in range(9)}
    stages["0"]["status"] = "running"
    for i in ((6, 7, 8) if investigation else (() if enabled else (8,))):
        stages[str(i)] = {"status": "skipped", "artifacts": [], "verdict": "NOT_APPLICABLE"}
    state = {
        "schema_version": "vector-opt-run.v2", "workflow": args.workflow,
        "run_id": args.run_id, "repo": str(repo), "created_at": now, "updated_at": now,
        "source": {"baseline_commit": git_commit(repo), "candidate_commit": None},
        "contract": {"manifest": None, "frozen": False},
        "experiment": {"experiment_root": str(root), "experiment_id": experiment_id,
                       "run_dir": str(run_dir),
                       "wiki_root": str(args.wiki_root.resolve()) if enabled else None},
        "stages": stages, "selected_candidate": None,
        "results": {"correctness": None, "datasets": {}, "geomean_speedup": None,
                    "verdict": None, "commit_status": "not_authorized"},
        "decisions": [],
        "knowledge_sync": {"status": "pending" if enabled else "not_applicable",
                           "raw_path": None, "experiment_page": None, "decision_page": None,
                           "receipt": None, "validation_receipt": None},
    }
    from validate_state import validate
    path = run_dir / "state.json"
    try:
        validate(state, path)
        run_dir.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
