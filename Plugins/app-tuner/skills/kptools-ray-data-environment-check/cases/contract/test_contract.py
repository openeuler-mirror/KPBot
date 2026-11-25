#!/usr/bin/env python3
"""Executable tier-A contract tests using standard-library mocks."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_environment", ROOT / "scripts/check_environment.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EnvironmentContract(unittest.TestCase):
    def test_probe_shape_and_statuses(self):
        for function in (
            MODULE.ready("x", {"a": 1}),
            MODULE.blocked("x", "bad", {}),
            MODULE.unknown("x", "why", {}),
        ):
            self.assertEqual(
                set(function),
                {"name", "status", "mandatory", "reason", "evidence"},
            )
            self.assertTrue(function["mandatory"])
        self.assertEqual(MODULE.ready("x", {})["status"], "ready")

    def test_exact_package_version(self):
        with mock.patch.object(
            MODULE.importlib.metadata, "version", return_value="0.0.1"
        ):
            result = MODULE.package_probe("ray")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "wrong_version")
        self.assertEqual(result["evidence"]["actual"], "0.0.1")

    def test_missing_package(self):
        with mock.patch.object(
            MODULE.importlib.metadata,
            "version",
            side_effect=MODULE.importlib.metadata.PackageNotFoundError,
        ):
            self.assertEqual(
                MODULE.package_probe("ray")["reason"], "not_installed"
            )

    def test_insufficient_shm(self):
        usage = mock.Mock(total=1024, free=1024)
        with mock.patch.object(
            MODULE.shutil, "disk_usage", return_value=usage
        ):
            result = MODULE.shm_probe()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "insufficient_shm")

    def test_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text("keep", encoding="utf-8")
            self.assertEqual(
                MODULE.output_probe(output)["reason"], "already_exists"
            )
            with self.assertRaises(FileExistsError):
                MODULE.write_exclusive(output, {})
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_new_output_and_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "a/b/report.json"
            self.assertEqual(MODULE.output_probe(output)["status"], "ready")
            MODULE.write_exclusive(output, {"ok": True})
            self.assertTrue(
                json.loads(output.read_text(encoding="utf-8"))["ok"]
            )

    def test_overall_blocked_dominates_unknown(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                MODULE,
                "platform_probe",
                return_value=MODULE.blocked("linux", "no", {}),
            ),
            mock.patch.object(
                MODULE,
                "python_probe",
                return_value=MODULE.unknown("python", "unknown", {}),
            ),
        ):
            report = MODULE.collect(Path(directory) / "new.json")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["summary"]["unknown"], 3)
        self.assertIn("not production", " ".join(report["limitations"]))

    def test_native_operation_is_real_assertion(self):
        fake_numpy = mock.MagicMock()
        fake_numpy.arange.return_value = mock.MagicMock()
        fake_pyarrow = mock.Mock()
        table = fake_pyarrow.Table.from_arrays.return_value
        table.column.return_value.to_pylist.return_value = list(
            range(1, 16, 2)
        )
        with mock.patch.object(
            MODULE.importlib,
            "import_module",
            side_effect=[fake_numpy, fake_pyarrow],
        ):
            result = MODULE.native_operations_probe()
        self.assertEqual(result["status"], "ready")
        fake_numpy.arange.assert_called_once()

    def test_source_has_no_runtime_or_install_actions(self):
        source = (ROOT / "scripts/check_environment.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "ray.init(",
            "ray.shutdown(",
            "subprocess",
            "pip install",
            "os.system",
        ):
            self.assertNotIn(forbidden, source)

    def test_skill_metadata_and_eval_specs(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("license: MulanPSL-2.0", skill)
        self.assertIn("metadata:\n  compatibility:", skill)
        self.assertNotIn("\ncompatibility:", skill)
        evaluations = json.loads(
            (ROOT / "evals/evals.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            evaluations["skill_name"], "kptools-ray-data-environment-check"
        )
        self.assertEqual(len(evaluations["evals"]), 2)
        self.assertTrue(
            all(item["status"] == "not_run" for item in evaluations["evals"])
        )
        queries = json.loads(
            (ROOT / "evals/eval_queries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(queries["skill_name"], evaluations["skill_name"])
        self.assertEqual(len(queries["queries"]), 3)
        self.assertTrue(
            all(
                item["prompt"] and isinstance(item["should_trigger"], bool)
                for item in queries["queries"]
            )
        )
        self.assertTrue(
            all(
                item["query"] and item["expected"]
                for item in evaluations["evals"]
            )
        )


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        EnvironmentContract
    )
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    output = ROOT / "results/contract/result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "success": outcome.wasSuccessful(),
                "tests_run": outcome.testsRun,
                "contract_mocks_are_e2e": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
