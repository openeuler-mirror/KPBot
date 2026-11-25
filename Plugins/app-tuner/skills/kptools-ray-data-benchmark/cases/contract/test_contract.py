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


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="ascii")


def nested_v2_fixture(root):
    write(root / "proc/self/cgroup", "0::/service\n")
    write(
        root / "proc/self/mountinfo",
        "30 20 0:26 /system.slice /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
    )
    write(root / "proc/meminfo", "MemAvailable: 8388608 kB\n")
    base = root / "sys/fs/cgroup"
    write(base / "cpu.max", "400000 100000")
    write(base / "memory.max", "1073741824")
    write(base / "memory.current", "0")
    write(base / "service/cpu.max", "100000 100000")
    write(base / "service/memory.max", "536870912")
    write(base / "service/memory.current", "402653184")


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

    def test_preflight_enforces_nested_v2_cpu_and_memory_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            output = Path(directory) / "output"
            output.mkdir()
            nested_v2_fixture(root)
            usage = mock.Mock(
                total=2 * MODULE.OBJECT_STORE_BYTES,
                free=2 * MODULE.OBJECT_STORE_BYTES,
            )
            with (
                mock.patch.object(
                    MODULE.importlib.metadata,
                    "version",
                    side_effect=lambda package: MODULE.EXPECTED[package],
                ),
                mock.patch.object(
                    MODULE.os, "sched_getaffinity", return_value=set(range(8))
                ),
                mock.patch.object(MODULE.os, "cpu_count", return_value=8),
                mock.patch.object(MODULE.shutil, "disk_usage", return_value=usage),
            ):
                problems, evidence = MODULE.preflight(
                    self.config(output), output, root
                )
        self.assertIn("at least two visible CPUs are required", problems)
        self.assertIn("available memory is smaller than the object store", problems)
        self.assertEqual(evidence["visible_cpus"], 1.0)
        self.assertEqual(evidence["cpu_visibility"]["cgroup_quotas"], [1.0, 4.0])
        self.assertEqual(
            evidence["cgroup_remaining_by_group_bytes"],
            [128 * 1024 * 1024, 1024 * 1024 * 1024],
        )

    def test_root_v1_cgroup_limits_are_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "proc/self/cgroup",
                "1:cpu,cpuacct:/\n2:memory:/\n",
            )
            write(
                root / "proc/self/mountinfo",
                "30 20 0:26 / /sys/fs/cgroup/cpu rw - cgroup cgroup rw,cpu,cpuacct\n"
                "31 20 0:27 / /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n",
            )
            write(root / "sys/fs/cgroup/cpu/cpu.cfs_quota_us", "100000")
            write(root / "sys/fs/cgroup/cpu/cpu.cfs_period_us", "100000")
            write(root / "sys/fs/cgroup/memory/memory.limit_in_bytes", "134217728")
            write(root / "sys/fs/cgroup/memory/memory.usage_in_bytes", "0")
            self.assertEqual(MODULE.cgroup_cpu_quotas(root), [1.0])
            self.assertEqual(
                MODULE.cgroup_memory_remaining(root), [128 * 1024 * 1024]
            )

    def test_preflight_preserves_unreadable_package_metadata_evidence(self):
        errors = (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid metadata"),
            OSError("metadata I/O failed"),
        )
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(
                    MODULE.importlib.metadata, "version", side_effect=error
                ),
            ):
                output = Path(directory)
                problems, evidence = MODULE.preflight(
                    self.config(output), output, Path(directory) / "absent"
                )
                self.assertTrue(any("ray must be" in item for item in problems))
                self.assertIsNone(evidence["actual_versions"]["ray"])
                self.assertEqual(
                    evidence["package_metadata_errors"]["ray"]["error_type"],
                    type(error).__name__,
                )

    def test_worker_shutdown_failure_controls_final_exit_and_result(self):
        class FailingShutdownRay(FakeRay):
            def shutdown(self):
                super().shutdown()
                raise RuntimeError("shutdown failed")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ray = FailingShutdownRay()
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
            self.assertEqual(returncode, 1)
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("shutdown failed", result["shutdown_error"])
            self.assertEqual(ray.shutdown_count, 1)

    def test_worker_rejects_mismatched_parent_output_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            other = Path(directory) / "other"
            output.mkdir()
            other.mkdir()
            config = self.config(other)
            config_path = output / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            result_path = other / "result.json"
            result_path.write_text("keep", encoding="utf-8")
            args = MODULE.parse_args(
                [
                    "--output-dir",
                    str(output),
                    "--worker-config",
                    str(config_path),
                ]
            )
            self.assertEqual(MODULE.main_with_args(args), 2)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "keep")
            self.assertFalse((output / "result.json").exists())

    def test_worker_result_is_exclusive_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result_path = output / "result.json"
            result_path.write_text("keep", encoding="utf-8")
            with (
                mock.patch.object(
                    MODULE, "preflight", return_value=(["blocked"], {})
                ),
                self.assertRaises(FileExistsError),
            ):
                MODULE.worker(self.config(output))
            self.assertEqual(result_path.read_text(encoding="utf-8"), "keep")

    def test_worker_summary_excludes_warmup_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ray = FakeRay()
            with (
                mock.patch.dict(sys.modules, {"ray": ray}),
                mock.patch.dict(
                    MODULE.os.environ, {"RAY_ADDRESS": "unreachable.invalid"}
                ),
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
            self.assertEqual(ray.init_options["address"], "local")
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
                    MODULE.worker(self.config(Path(directory))), 1
                )
            result = json.loads(
                (Path(directory) / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("small shm", result["error"])

    def test_worker_reports_measured_versions_when_ready_and_blocked(self):
        actual = {"ray": "0.0.0", "numpy": None, "pyarrow": "mismatch"}
        for problems, status in (([], "completed"), (["mismatch"], "blocked")):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory)
                ray = FakeRay()
                with (
                    mock.patch.dict(sys.modules, {"ray": ray}),
                    mock.patch.object(
                        MODULE,
                        "preflight",
                        return_value=(problems, {"actual_versions": actual}),
                    ),
                ):
                    returncode = MODULE.worker(self.config(output))
                result = json.loads(
                    (output / "result.json").read_text(encoding="utf-8")
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(returncode, 0 if not problems else 1)
                expected = {
                    "python": MODULE.platform.python_version(),
                    **actual,
                }
                self.assertEqual(result["versions"], expected)
                self.assertNotEqual(
                    result["versions"],
                    {
                        "python": MODULE.platform.python_version(),
                        **MODULE.EXPECTED,
                    },
                )

    def test_parent_spawns_worker_with_allowlisted_environment(self):
        inherited = {
            "PATH": "/runtime/bin",
            "PYTHONPATH": "/runtime/python",
            "LD_LIBRARY_PATH": "/runtime/native",
            "OMP_NUM_THREADS": "1",
            "LD_PRELOAD": "must-not-reach-child",
            "RAY_ADDRESS": "unreachable.invalid",
            "UNRELATED_SECRET": "must-not-reach-child",
            "AWS_SECRET_ACCESS_KEY": "must-not-reach-child",
        }
        with tempfile.TemporaryDirectory() as directory:
            args = MODULE.parse_args(
                ["--output-dir", str(Path(directory) / "result")]
            )
            with (
                mock.patch.dict(MODULE.os.environ, inherited, clear=True),
                mock.patch.object(MODULE.subprocess, "Popen") as popen,
                mock.patch.object(MODULE, "terminate_owned_group"),
            ):
                popen.return_value.pid = 123
                popen.return_value.wait.return_value = 0
                self.assertEqual(MODULE.parent(args), 0)
            child_environment = popen.call_args.kwargs["env"]
            self.assertEqual(
                child_environment,
                {
                    "PATH": "/runtime/bin",
                    "PYTHONPATH": "/runtime/python",
                    "LD_LIBRARY_PATH": "/runtime/native",
                    "OMP_NUM_THREADS": "1",
                },
            )
            self.assertIsNot(child_environment, MODULE.os.environ)
            self.assertNotIn("UNRELATED_SECRET", child_environment)
            self.assertNotIn("RAY_ADDRESS", child_environment)
            self.assertNotIn("LD_PRELOAD", child_environment)

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
        with self.assertRaises(SystemExit) as invalid_rows:
            MODULE.parse_args(["--output-dir", "new", "--rows", "0"])
        self.assertEqual(invalid_rows.exception.code, 2)
        with self.assertRaises(SystemExit) as invalid_batch:
            MODULE.parse_args(
                ["--output-dir", "new", "--rows", "2", "--batch-size", "3"]
            )
        self.assertEqual(invalid_batch.exception.code, 2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing"
            output.mkdir()
            args = MODULE.parse_args(["--output-dir", str(output)])
            self.assertEqual(MODULE.parent(args), 2)

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
        self.assertEqual(
            evaluations["skill_name"], "kptools-ray-data-benchmark"
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
