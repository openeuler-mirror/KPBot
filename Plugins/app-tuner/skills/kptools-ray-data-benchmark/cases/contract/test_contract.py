#!/usr/bin/env python3
"""Executable tier-A lifecycle and measurement contract tests."""

import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "benchmark", ROOT / "scripts/benchmark.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Vector:
    def __init__(self, values):
        self.values = list(values)

    def __mul__(self, number):
        return Vector(value * number for value in self.values)

    def __add__(self, number):
        return Vector(value + number for value in self.values)


class FakeDataset:
    def __init__(self, rows):
        self.rows = rows
        self.materialize_count = 0
        self.marker = None

    def map_batches(self, function, fn_kwargs, **kwargs):
        self.marker = Path(fn_kwargs["marker"])
        transformed = function(
            {"id": Vector(range(self.rows))}, fn_kwargs["marker"]
        )
        self.records = [
            {"id": identifier, "value": value}
            for identifier, value in zip(
                transformed["id"].values, transformed["value"].values
            )
        ]
        return self

    def materialize(self):
        self.materialize_count += 1
        return self

    def take_all(self):
        return self.records


class FakeData:
    def __init__(self):
        self.datasets = []

    def range(self, rows, override_num_blocks):
        dataset = FakeDataset(rows)
        self.datasets.append(dataset)
        return dataset


class FakeRay:
    def __init__(self):
        self.data = FakeData()
        self.shutdown_count = 0

    def init(self, **kwargs):
        self.init_options = kwargs

    def shutdown(self):
        self.shutdown_count += 1


class BenchmarkContract(unittest.TestCase):
    def config(self, output):
        return {
            "output_dir": str(output),
            "rows": 8,
            "batch_size": 4,
            "parallelism": 2,
            "rounds": 3,
            "warmup_rounds": 1,
            "timeout_seconds": 30.0,
        }

    def test_materialize_once_and_exact_results(self):
        with tempfile.TemporaryDirectory() as directory:
            ray = FakeRay()
            phase = MODULE.run_phase(
                ray,
                self.config(Path(directory)),
                "measured-1",
                Path(directory),
            )
            dataset = ray.data.datasets[0]
            self.assertEqual(dataset.materialize_count, 1)
            self.assertEqual(phase["materialize_calls"], 1)
            self.assertEqual(phase["rows"], 8)
            self.assertEqual(phase["id_sum"], 28)
            self.assertEqual(phase["value_sum"], 64)
            self.assertFalse(phase["second_execution"])
            self.assertEqual(
                phase["udf_calls_at_materialize"],
                phase["udf_calls_after_validation"],
            )

    def test_second_execution_is_detected(self):
        class ReexecutingDataset(FakeDataset):
            def take_all(self):
                with self.marker.open("ab") as marker:
                    marker.write(b"x")
                return super().take_all()

        ray = FakeRay()
        ray.data.range = lambda rows, override_num_blocks: ReexecutingDataset(
            rows
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "re-executed"),
        ):
            MODULE.run_phase(
                ray,
                self.config(Path(directory)),
                "measured-1",
                Path(directory),
            )

    def test_worker_summary_excludes_warmup_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ray = FakeRay()
            with (
                mock.patch.dict(sys.modules, {"ray": ray}),
                mock.patch.object(
                    MODULE, "preflight", return_value=([], {"mock": True})
                ),
            ):
                returncode = MODULE.worker(self.config(output))
            result = json.loads(
                (output / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(returncode, 0)
            self.assertTrue(result["success"])
            self.assertEqual(len(result["phases"]), 4)
            self.assertEqual(result["summary"]["measured_rounds"], 3)
            self.assertTrue(result["summary"]["warmup_excluded"])
            self.assertEqual(ray.shutdown_count, 1)
            self.assertTrue(result["own_runtime_shutdown"])
            self.assertEqual(ray.init_options["num_cpus"], 2)
            self.assertEqual(
                ray.init_options["object_store_memory"],
                MODULE.OBJECT_STORE_BYTES,
            )

    def test_worker_persists_preflight_failure_without_init(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                MODULE, "preflight", return_value=(["small shm"], {})
            ):
                self.assertEqual(
                    MODULE.worker(self.config(Path(directory))), 2
                )
            result = json.loads(
                (Path(directory) / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("small shm", result["error"])

    def test_signal_only_owned_group(self):
        process = mock.Mock(pid=321)
        process.wait.side_effect = [subprocess.TimeoutExpired("x", 1), 0]
        with mock.patch.object(MODULE.os, "killpg") as killpg:
            MODULE.terminate_owned_group(process)
        killpg.assert_any_call(321, signal.SIGTERM)
        for call in killpg.call_args_list:
            self.assertEqual(call.args[0], 321)

    def test_timeout_is_finite_and_bounded(self):
        for invalid in ("nan", "inf", "0", "3601"):
            with self.assertRaises(Exception):
                MODULE.timeout_value(invalid)
        self.assertEqual(MODULE.timeout_value("120"), 120.0)

    def test_cli_defaults_and_bounds(self):
        args = MODULE.parse_args(["--output-dir", "new"])
        self.assertEqual(
            (args.rows, args.rounds, args.warmup_rounds), (256, 3, 1)
        )
        self.assertEqual((args.batch_size, args.parallelism), (32, 4))
        with self.assertRaises(SystemExit):
            MODULE.parse_args(["--output-dir", "new", "--rows", "0"])
        with self.assertRaises(SystemExit):
            MODULE.parse_args(
                ["--output-dir", "new", "--rows", "2", "--batch-size", "3"]
            )

    def test_source_has_no_global_cleanup_or_capacity_claim(self):
        source = (ROOT / "scripts/benchmark.py").read_text(encoding="utf-8")
        for forbidden in ("ray stop", "pkill", "killall", "sudo"):
            self.assertNotIn(forbidden, source)
        self.assertIn("start_new_session=True", source)

    def test_skill_metadata_and_eval_specs(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("license: MulanPSL-2.0", skill)
        self.assertIn("metadata:\n  compatibility:", skill)
        self.assertNotIn("\ncompatibility:", skill)
        evaluations = json.loads(
            (ROOT / "evals/evals.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(evaluations["scores"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BenchmarkContract)
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
