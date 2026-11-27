#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
from pathlib import Path


CASE_DIR = Path(__file__).resolve().parent
SKILL_DIR = CASE_DIR.parents[1]


def invoke(fixture, output):
    completed = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=output.parent,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)
    return json.loads(output.read_text(encoding="utf-8"))


def check_common_contract(fixture, temp_dir):
    invalid = temp_dir / "array.json"
    invalid.write_text("[]\n", encoding="utf-8")
    invalid_output = temp_dir / "array-output.json"
    invalid_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(invalid), "--output", str(invalid_output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=temp_dir,
    )

    fractional = json.loads(fixture.read_text(encoding="utf-8"))
    fractional["thresholds"]["min_samples_per_cohort"] = 2.9
    fractional_path = temp_dir / "fractional.json"
    fractional_path.write_text(json.dumps(fractional), encoding="utf-8")
    fractional_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fractional_path), "--output", str(temp_dir / "fractional-output.json")],
        check=False,
        capture_output=True,
        text=True,
        cwd=temp_dir,
    )

    race_output = temp_dir / "race.json"
    processes = [
        subprocess.Popen(
            [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(race_output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=temp_dir,
        )
        for _ in range(6)
    ]
    return_codes = []
    for process in processes:
        process.communicate()
        return_codes.append(process.returncode)
    existing_output = temp_dir / "existing.json"
    sentinel = b'{"sentinel":true}\n'
    existing_output.write_bytes(sentinel)
    existing_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(existing_output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=temp_dir,
    )
    arbitrary_output = temp_dir / "arbitrary.json"
    arbitrary_sentinel = b'not a decision\n'
    arbitrary_output.write_bytes(arbitrary_sentinel)
    arbitrary_force_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(arbitrary_output), "--force"],
        check=False,
        capture_output=True,
        text=True,
        cwd=temp_dir,
    )
    valid_force_output = temp_dir / "valid-force.json"
    invoke(fixture, valid_force_output)
    valid_force_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(valid_force_output), "--force"],
        check=False,
        capture_output=True,
        text=True,
        cwd=temp_dir,
    )
    workspace = temp_dir / "workspace"
    workspace.mkdir()
    outside_output = temp_dir / "outside.json"
    outside_run = subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(outside_output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=workspace,
    )
    outside_dir = temp_dir / "outside-dir"
    outside_dir.mkdir()
    linked_parent = workspace / "linked"
    symlink_parent_rejected = True
    try:
        linked_parent.symlink_to(outside_dir, target_is_directory=True)
    except (NotImplementedError, OSError):
        pass
    else:
        linked_output = linked_parent / "decision.json"
        linked_run = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(fixture), "--output", str(linked_output)],
            check=False,
            capture_output=True,
            text=True,
            cwd=workspace,
        )
        symlink_parent_rejected = linked_run.returncode == 2 and not (outside_dir / "decision.json").exists()
    return {
        "array_rejected": invalid_run.returncode == 2 and "Traceback" not in invalid_run.stderr and not invalid_output.exists(),
        "fractional_min_rejected": fractional_run.returncode == 2 and "Traceback" not in fractional_run.stderr,
        "race_preserved": return_codes.count(0) == 1 and return_codes.count(2) == 5 and race_output.is_file(),
        "existing_preserved": existing_run.returncode == 2 and existing_output.read_bytes() == sentinel,
        "arbitrary_force_preserved": arbitrary_force_run.returncode == 2 and arbitrary_output.read_bytes() == arbitrary_sentinel,
        "valid_force_replaced": valid_force_run.returncode == 0,
        "outside_cwd_rejected": outside_run.returncode == 2 and not outside_output.exists(),
        "symlink_parent_rejected": symlink_parent_rejected,
    }


def main():
    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        confirmed = invoke(CASE_DIR / "fixtures" / "confirmed.json", temp_dir / "confirmed.json")
        outcome = invoke(CASE_DIR / "fixtures" / "outcome-metric.json", temp_dir / "outcome.json")
        invalid_mode = json.loads((CASE_DIR / "fixtures" / "confirmed.json").read_text(encoding="utf-8"))
        invalid_mode["benchmark_mode"] = "unknown"
        invalid_mode_path = temp_dir / "invalid-mode.json"
        invalid_mode_path.write_text(json.dumps(invalid_mode), encoding="utf-8")
        invalid_mode_run = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "classify.py"), "--input", str(invalid_mode_path), "--output", str(temp_dir / "invalid-mode-output.json")],
            check=False,
            capture_output=True,
            text=True,
            cwd=temp_dir,
        )
        common = check_common_contract(CASE_DIR / "fixtures" / "confirmed.json", temp_dir)
    result = {
        "success": confirmed["status"] == "confirmed"
        and outcome["status"] == "blocked"
        and invalid_mode_run.returncode == 2
        and all(common.values()),
        "confirmed_status": confirmed["status"],
        "outcome_metric_status": outcome["status"],
        "invalid_mode_rejected": invalid_mode_run.returncode == 2,
        **common,
    }
    results_dir = CASE_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
