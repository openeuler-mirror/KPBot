#!/usr/bin/env python3
"""Focused contract tests for collect_result parsing and validation."""

from __future__ import annotations

import sys
import tempfile
import unittest
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from collect_result import ContractValidator, collect_target_result, read_json  # noqa: E402


def valid_completion_gate() -> dict[str, object]:
    return {
        "pipeline_reached_final_report": True,
        "required_stages_seen": True,
        "patch_collected": True,
        "functional_verified": True,
        "performance_measured": True,
        "artifact_consistent": True,
        "patch_hygiene_passed": True,
        "evidence_sources": {
            "structured_json": [],
            "batch_result": [],
            "baseline_blocked": [],
            "verification": [],
            "stage_sources": {},
        },
    }


def run_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stdout}")


def init_repo(path: Path) -> None:
    run_git(["init"], path)
    run_git(["config", "user.email", "test@example.invalid"], path)
    run_git(["config", "user.name", "Collect Result Test"], path)


class CollectResultContractTests(unittest.TestCase):
    def test_completion_gate_accepts_complete_contract(self) -> None:
        gate = valid_completion_gate()

        self.assertIs(ContractValidator.validate_completion_gate(gate), gate)

    def test_completion_gate_rejects_missing_required_field(self) -> None:
        gate = valid_completion_gate()
        del gate["functional_verified"]

        with self.assertRaisesRegex(ValueError, "missing required field"):
            ContractValidator.validate_completion_gate(gate)

    def test_completion_gate_rejects_bool_type_mismatch(self) -> None:
        gate = valid_completion_gate()
        gate["patch_collected"] = "yes"

        with self.assertRaisesRegex(ValueError, "expected bool"):
            ContractValidator.validate_completion_gate(gate)

    def test_completion_gate_rejects_evidence_sources_type_mismatch(self) -> None:
        gate = valid_completion_gate()
        gate["evidence_sources"] = []

        with self.assertRaisesRegex(ValueError, "expected dict field"):
            ContractValidator.validate_completion_gate(gate)

    def test_read_json_strict_rejects_non_dict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text("[1, 2, 3]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "sample_contract: expected dict"):
                read_json(path, strict=True, contract_name="sample_contract")
            self.assertEqual(read_json(path), {})

    def test_read_json_strict_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "payload.json"
            path.write_text("{broken\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "sample_contract: invalid JSON"):
                read_json(path, strict=True, contract_name="sample_contract")
            self.assertEqual(read_json(path), {})

    def test_collect_target_result_rejects_invalid_previous_result_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            out.mkdir()
            (out / "target_result.json").write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "target_result: expected dict"):
                collect_target_result(
                    target_id="kernel",
                    workdir=repo,
                    target_out=out,
                    runner_result={"status": "completed"},
                )

    def test_collect_target_result_writes_contracts_for_minimal_normal_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            out = root / "out"
            repo.mkdir()
            init_repo(repo)
            (repo / "kernel.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            run_git(["add", "-A"], repo)
            run_git(["commit", "-m", "baseline"], repo)
            run_git(["tag", "batch_baseline"], repo)
            reports = repo / "optimization_reports" / "run_1"
            stages = reports / "stages"
            points = reports / "points"
            stages.mkdir(parents=True)
            points.mkdir()
            for stage in [
                "GatherContext",
                "ParseIntent",
                "PrepareProject",
                "DecomposeTasks",
                "AnalyzeTestcase",
                "AnalyzeHotspot",
            ]:
                (stages / f"{stage.lower()}.json").write_text('{"status":"ok"}\n', encoding="utf-8")
            (reports / "FINAL_SUMMARY.md").write_text("# Final\n\nPipeline Complete\n", encoding="utf-8")
            (reports / "batch_result.json").write_text(
                '{"status":"complete_no_optimization","pipeline_status":"completed",'
                '"applied_count":0,"verified_count":0}\n',
                encoding="utf-8",
            )

            result = collect_target_result(
                target_id="kernel",
                workdir=repo,
                target_out=out,
                runner_result={"status": "completed", "final_marker_seen": True},
            )

            self.assertEqual(result["status"], "complete_no_optimization")
            self.assertEqual(result["completion_status"], "completed")
            self.assertTrue((out / "completion_gate.json").exists())
            self.assertTrue((out / "target_result.json").exists())
            self.assertTrue(result["completion_gate"]["pipeline_reached_final_report"])
            self.assertTrue(result["completion_gate"]["required_stages_seen"])

    def test_collect_target_result_rejects_missing_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            with self.assertRaisesRegex(FileNotFoundError, "workdir not found"):
                collect_target_result(
                    target_id="missing",
                    workdir=root / "missing_repo",
                    target_out=root / "out",
                    runner_result={"status": "completed"},
                )

    def test_collect_target_result_rejects_runner_result_type_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()

            with self.assertRaisesRegex(ValueError, "runner_result: expected dict"):
                collect_target_result(
                    target_id="kernel",
                    workdir=repo,
                    target_out=root / "out",
                    runner_result=[],  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
