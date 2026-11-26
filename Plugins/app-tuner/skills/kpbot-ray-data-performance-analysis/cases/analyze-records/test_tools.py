#!/usr/bin/env python3
"""Synthetic, Ray-free regression tests; not a real workload benchmark."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest


SKILL = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, SKILL / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_boundary_check():
    spec = importlib.util.spec_from_file_location(
        "real_ray_check", SKILL / "cases" / "real-ray" / "check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


environment = load("inspect_environment")
comparison = load("compare_runs")
runner = load("record_run")


def document(workload="table-etl", unit="rows/s", direction="higher"):
    return {
        "schema_version": 1,
        "metric": {"name": "throughput", "unit": unit, "direction": direction},
        "context": {
            "workload": workload,
            "input_id": "synthetic-seed-42-count-100",
            "scope": "read_to_sink_complete",
            "resources": {"cpus": 2},
            "software": {"ray": "synthetic-version"},
            "cache_state": "warm",
            "config": {"batch_size": 16},
            "correctness_contract": "keyed-output-equal",
        },
        "samples": [
            {
                "value": value,
                "success": True,
                "correct": True,
                "evidence": f"synthetic-run-{index}.json",
            }
            for index, value in enumerate((100, 102, 98))
        ],
    }


class ComparisonTests(unittest.TestCase):
    def test_three_workload_units(self):
        for name, unit in (
            ("table-etl", "rows/s"),
            ("text-batch", "tokens/s"),
            ("image-preprocess", "samples/s"),
        ):
            with self.subTest(workload=name):
                before = document(name, unit)
                after = copy.deepcopy(before)
                for sample in after["samples"]:
                    sample["value"] *= 1.5
                result = comparison.compare(before, after)
                self.assertTrue(result["comparable"])
                self.assertAlmostEqual(result["improvement_percent"], 50)
                self.assertEqual(result["baseline"]["values"], [100, 102, 98])

    def test_latency_reduction_is_not_throughput_gain(self):
        before = document(unit="s", direction="lower")
        before["metric"]["name"] = "elapsed"
        after = copy.deepcopy(before)
        for sample in after["samples"]:
            sample["value"] /= 2
        result = comparison.compare(before, after)
        self.assertEqual(result["speedup"], 2)
        self.assertEqual(result["improvement_percent"], 50)

    def test_incompatible_conditions_refuse_gain(self):
        for field in (
            "input_id",
            "scope",
            "resources",
            "cache_state",
            "correctness_contract",
            "workload",
            "software",
            "config",
        ):
            with self.subTest(field=field):
                before, after = document(), document()
                after["context"][field] = "different"
                result = comparison.compare(before, after)
                self.assertFalse(result["comparable"])
                self.assertIsNone(result["speedup"])

    def test_only_explicit_factor_allowed(self):
        before, after = document(), document()
        after["context"]["config"]["batch_size"] = 32
        result = comparison.compare(before, after, ["config"])
        self.assertTrue(result["comparable"])
        self.assertEqual(
            result["changes"]["config"]["candidate"], {"batch_size": 32}
        )

    def test_resource_change_cannot_be_whitelisted(self):
        with self.assertRaises(ValueError):
            comparison.compare(document(), document(), ["resources"])

    def test_unknown_extra_condition_is_not_ignored(self):
        item = document()
        item["context"]["storage"] = None
        self.assertFalse(comparison.compare(item, item)["comparable"])

    def test_missing_or_unknown_context(self):
        for unknown in (None, "unknown", {}, {"cpus": None}):
            with self.subTest(value=unknown):
                before, after = document(), document()
                before["context"]["resources"] = unknown
                after["context"]["resources"] = unknown
                self.assertFalse(
                    comparison.compare(before, after)["comparable"]
                )

    def test_metric_mismatch(self):
        before, after = document(), document()
        after["metric"]["unit"] = "bytes/s"
        self.assertFalse(comparison.compare(before, after)["comparable"])

    def test_invalid_metric_contract(self):
        for metric in (
            {"name": "throughput", "unit": "rows/s", "direction": "sideways"},
            {"name": "throughput", "unit": "", "direction": "higher"},
            {"name": "throughput", "direction": "higher"},
            {"unit": "rows/s", "direction": "higher"},
        ):
            with self.subTest(metric=metric):
                item = document()
                item["metric"] = metric
                with self.assertRaises(ValueError):
                    comparison.compare(item, item)

    def test_lower_is_better_regression_is_negative(self):
        before = document(unit="s", direction="lower")
        before["metric"]["name"] = "elapsed"
        after = copy.deepcopy(before)
        for sample in after["samples"]:
            sample["value"] *= 2
        result = comparison.compare(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["speedup"], 0.5)
        self.assertEqual(result["improvement_percent"], -100)

    def test_failed_unknown_or_missing_evidence(self):
        for field, value in (
            ("success", False),
            ("correct", None),
            ("evidence", ""),
        ):
            with self.subTest(field=field):
                after = document()
                after["samples"][1][field] = value
                result = comparison.compare(document(), after)
                self.assertFalse(result["comparable"])
                self.assertEqual(result["candidate"]["n"], 3)

    def test_invalid_numbers(self):
        for value in (0, -1, True, float("nan"), float("inf"), "100"):
            with self.subTest(value=value):
                after = document()
                after["samples"][0]["value"] = value
                with self.assertRaises(ValueError):
                    comparison.compare(document(), after)

    def test_single_sample_does_not_invent_variation(self):
        item = document()
        item["samples"] = item["samples"][:1]
        result = comparison.compare(item, item)
        self.assertIsNone(result["baseline"]["cv"])
        self.assertTrue(result["warnings"])

    def test_variation_not_discarded(self):
        item = document()
        item["samples"][2]["value"] = 1000
        result = comparison.compare(item, item)
        self.assertEqual(result["baseline"]["max"], 1000)
        self.assertGreater(result["baseline"]["cv"], 1)

    def test_missing_schema_and_samples(self):
        for item in ({}, {"schema_version": 99}, []):
            with self.subTest(item=item), self.assertRaises(ValueError):
                comparison.compare(item, document())
        item = document()
        item["samples"] = []
        with self.assertRaises(ValueError):
            comparison.compare(item, document())

    def test_cli_refusal_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, after = document(), document()
            after["context"]["input_id"] = "other"
            (root / "before.json").write_text(
                json.dumps(before), encoding="utf-8"
            )
            (root / "after.json").write_text(
                json.dumps(after), encoding="utf-8"
            )
            command = [
                sys.executable,
                str(SKILL / "scripts/compare_runs.py"),
                str(root / "before.json"),
                str(root / "after.json"),
                "--out",
                str(root / "comparison.json"),
            ]
            result = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            saved = (root / "comparison.json").read_bytes()
            self.assertIsNone(json.loads(saved)["improvement_percent"])
            result = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((root / "comparison.json").read_bytes(), saved)

    def test_cli_invalid_json_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "before.json").write_text("{not json", encoding="utf-8")
            (root / "after.json").write_text(
                json.dumps(document()), encoding="utf-8"
            )
            output = root / "comparison.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "scripts/compare_runs.py"),
                    str(root / "before.json"),
                    str(root / "after.json"),
                    "--out",
                    str(output),
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(output.exists())


class EnvironmentTests(unittest.TestCase):
    def test_v2_mount_root_and_ancestor_limit(self):
        membership = "0::/tenant/job\n"
        mountinfo = "10 9 0:28 /tenant /cg rw - cgroup2 cgroup rw\n"
        files = {
            "/cg/job/cpu.max": "max 100000",
            "/cg/cpu.max": "150000 100000",
            "/cg/job/cpuset.cpus.effective": "2-3",
        }
        result = environment.cgroup_snapshot(
            membership, mountinfo, lambda path: files.get(path.as_posix())
        )
        self.assertEqual(result["cpu_quota_upper_bound"], 1.5)
        self.assertEqual(result["groups"][0]["cpuset.cpus.effective"], "2-3")

    def test_v1_cpu_controller(self):
        membership = "4:cpu,cpuacct:/job\n5:memory:/job\n"
        mountinfo = "10 9 0:28 / /cg rw - cgroup cpu rw,cpu,cpuacct\n"
        files = {
            "/cg/job/cpu.cfs_quota_us": "50000",
            "/cg/job/cpu.cfs_period_us": "100000",
        }
        result = environment.cgroup_snapshot(
            membership, mountinfo, lambda path: files.get(path.as_posix())
        )
        self.assertEqual(result["cpu_quota_upper_bound"], 0.5)
        self.assertEqual(len(result["groups"]), 1)

    def test_no_guessed_path_when_outside_mount(self):
        result = environment.cgroup_snapshot(
            "0::/other/job",
            "10 9 0:28 /tenant /cg rw - cgroup2 cgroup rw",
            lambda path: self.fail(f"Must not read guessed path {path}"),
        )
        self.assertEqual(result["groups"], [])
        self.assertIsNone(result["cpu_quota_upper_bound"])

    def test_missing_malformed_and_unlimited_quota(self):
        for value in (None, "", "max 100000", "-1 100000", "100 0", "garbage"):
            with self.subTest(value=value):
                self.assertIsNone(environment.quota_value("cgroup2", value))

    def test_v1_unlimited_and_missing_quota(self):
        for quota, period in (
            ("-1", "100000"),
            (None, "100000"),
            ("50000", None),
            ("0", "100000"),
        ):
            with self.subTest(quota=quota, period=period):
                self.assertIsNone(
                    environment.quota_value("cgroup", quota, period)
                )

    def test_escaped_mount_and_traversal(self):
        mountinfo = "10 9 0:28 / /my\\040cg rw - cgroup2 cgroup rw"
        result = environment.cgroup_locations("0::/job", mountinfo)
        self.assertEqual(result[0][1].as_posix(), "/my cg/job")
        self.assertEqual(
            environment.cgroup_locations("0::/../job", mountinfo), []
        )

    def test_live_snapshot_serializes_without_ray_import(self):
        result = environment.collect()
        json.dumps(result, allow_nan=False)
        self.assertIn("ray", result["packages"])
        self.assertNotIn("ray", sys.modules)
        self.assertNotIn("environ", result)

    def test_snapshot_cli_creates_parent_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "fresh-install/results/environment.json"
            self.assertFalse(output.parent.exists())
            command = [
                sys.executable,
                str(SKILL / "scripts/inspect_environment.py"),
                "--out",
                str(output),
            ]
            first = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = output.read_bytes()
            self.assertEqual(
                json.loads(saved)["scope"],
                "local_process_and_visible_cgroups_only",
            )
            second = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(second.returncode, 1)
            self.assertEqual(output.read_bytes(), saved)


class RealRayShmBoundaryTests(unittest.TestCase):
    """Ray-free checks that preflight matches the requested object store."""

    MiB = 1024 * 1024

    def setUp(self):
        self.check = load_boundary_check()

    def test_case_module_does_not_import_ray(self):
        self.assertNotIn("ray", sys.modules)

    def test_object_store_constant_is_shared(self):
        expected = 256 * self.MiB
        self.assertEqual(self.check.OBJECT_STORE_MEMORY_BYTES, expected)
        options = self.check.ray_init_options("/tmp/session")
        self.assertEqual(options["object_store_memory"], expected)
        self.assertEqual(options["_temp_dir"], "/tmp/session")

    def test_free_between_old_and_new_thresholds_is_rejected(self):
        total = 1024 * self.MiB
        for free in (128 * self.MiB, 192 * self.MiB, 256 * self.MiB - 1):
            with self.subTest(free=free):
                usage = SimpleNamespace(total=total, free=free, used=0)
                self.assertIsNotNone(self.check.shm_problem(usage))

    def test_adequate_free_memory_passes(self):
        for free in (256 * self.MiB, 512 * self.MiB):
            with self.subTest(free=free):
                usage = SimpleNamespace(
                    total=1024 * self.MiB, free=free, used=0
                )
                self.assertIsNone(self.check.shm_problem(usage))
        usage = SimpleNamespace(
            total=256 * self.MiB, free=256 * self.MiB, used=0
        )
        self.assertIsNone(self.check.shm_problem(usage))

    def test_total_below_object_store_is_rejected(self):
        usage = SimpleNamespace(
            total=256 * self.MiB - 1, free=256 * self.MiB - 1, used=0
        )
        self.assertIsNotNone(self.check.shm_problem(usage))

    def test_preflight_reports_boundary_as_environment_problem(self):
        total = 1024 * self.MiB
        usage = SimpleNamespace(total=total, free=192 * self.MiB, used=0)
        original = self.check.shutil.disk_usage
        self.check.shutil.disk_usage = lambda path: usage
        try:
            problems = self.check.preflight()
        finally:
            self.check.shutil.disk_usage = original
        self.assertTrue(any("/dev/shm" in item for item in problems), problems)

    def _run_main(self, module):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return module.main()

    def test_preflight_problem_exits_three_without_ray(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = self.check
            original_preflight, original_root = module.preflight, module.ROOT
            module.preflight = lambda: ["synthetic missing prerequisite"]
            module.ROOT = Path(temporary)
            try:
                self.assertNotIn("ray", sys.modules)
                code = self._run_main(module)
            finally:
                module.preflight, module.ROOT = (
                    original_preflight,
                    original_root,
                )
            saved = json.loads(
                (Path(temporary) / "results/real-ray/result.json").read_text()
            )
        self.assertEqual(code, 3)
        self.assertTrue(saved["env_not_satisfied"])
        self.assertFalse(saved["success"])
        self.assertNotIn("error", saved)

    def test_ray_init_failure_is_not_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = self.check
            fake_ray = ModuleType("ray")
            fake_ray.__version__ = "synthetic"

            def fail_init(**options):
                self.assertEqual(
                    options["object_store_memory"],
                    module.OBJECT_STORE_MEMORY_BYTES,
                )
                raise RuntimeError("synthetic object store allocation failure")

            fake_ray.init = fail_init
            fake_ray.shutdown = lambda: None
            original_preflight, original_root = module.preflight, module.ROOT
            original_run, original_ray = (
                module.subprocess.run,
                sys.modules.get("ray"),
            )
            module.preflight = lambda: []
            module.ROOT = Path(temporary)
            module.subprocess.run = lambda *args, **kwargs: None
            sys.modules["ray"] = fake_ray
            try:
                code = self._run_main(module)
            finally:
                module.preflight, module.ROOT = (
                    original_preflight,
                    original_root,
                )
                module.subprocess.run = original_run
                if original_ray is None:
                    sys.modules.pop("ray", None)
                else:
                    sys.modules["ray"] = original_ray
            saved = json.loads(
                (Path(temporary) / "results/real-ray/result.json").read_text()
            )
        self.assertEqual(code, 1)
        self.assertIn(
            "synthetic object store allocation failure", saved["error"]
        )
        self.assertNotIn("env_not_satisfied", saved)


class RunnerTests(unittest.TestCase):
    def test_success_and_literal_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            result = runner.record(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1])",
                    "$(echo secret);*",
                ],
                root,
                10,
            )
            self.assertTrue(result["success"])
            self.assertEqual(
                (root / "stdout.log").read_text().strip(), "$(echo secret);*"
            )
            self.assertEqual(result["correctness"], "unknown")
            self.assertGreater(result["elapsed_seconds"], 0)

    def test_owner_only_modes_despite_permissive_umask(self):
        previous = os.umask(0)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                result = runner.record(
                    [sys.executable, "-c", "print('ok')"], root, 10
                )
                self.assertTrue(result["success"])
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                for name in ("stdout.log", "stderr.log", "run.json"):
                    self.assertEqual(
                        stat.S_IMODE((root / name).stat().st_mode),
                        0o600,
                        name,
                    )
        finally:
            os.umask(previous)

    def test_elapsed_excludes_log_setup_and_json_write(self):
        original_binary = runner.private_binary
        original_text = runner.private_text
        delay = 0.3

        def slow_binary(path):
            time.sleep(delay)
            return original_binary(path)

        def slow_text(path):
            time.sleep(delay)
            return original_text(path)

        runner.private_binary = slow_binary
        runner.private_text = slow_text
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                wall_start = time.perf_counter()
                result = runner.record(
                    [sys.executable, "-c", "import time;time.sleep(0.1)"],
                    root,
                    10,
                )
                wall = time.perf_counter() - wall_start
        finally:
            runner.private_binary = original_binary
            runner.private_text = original_text
        self.assertTrue(result["success"])
        # Two log opens and the run.json write each sleep; only the child
        # runtime belongs in elapsed_seconds.
        self.assertGreaterEqual(wall, 3 * delay + 0.1)
        self.assertLess(result["elapsed_seconds"], 2 * delay)

    def test_failed_command_and_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            result = runner.record(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('failed',file=sys.stderr);sys.exit(7)",
                ],
                root,
                10,
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["returncode"], 7)
            self.assertIn("failed", (root / "stderr.log").read_text())

    def test_start_failure_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            result = runner.record([str(root / "does-not-exist")], root, 10)
            self.assertFalse(result["success"])
            self.assertTrue(result["error"])
            self.assertTrue((root / "run.json").exists())

    def test_timeout_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            result = runner.record(
                [sys.executable, "-c", "import time;time.sleep(30)"], root, 0.2
            )
            self.assertTrue(result["timed_out"])
            self.assertFalse(result["success"])
            self.assertIsNotNone(result["returncode"])

    @unittest.skipUnless(os.name == "posix", "POSIX process group check")
    def test_timeout_stops_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            marker = Path(temporary) / "unexpected-child-output"
            child = (
                "import time;from pathlib import Path;time.sleep(1);"
                f"Path({str(marker)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(30)"
            )
            runner.record([sys.executable, "-c", parent], root, 0.3)
            import time

            time.sleep(1.2)
            self.assertFalse(marker.exists())

    def test_existing_directory_not_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sentinel").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                runner.record([sys.executable, "-c", "pass"], root, 10)
            self.assertEqual(
                [path.name for path in root.iterdir()], ["sentinel"]
            )

    def test_invalid_library_arguments_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            with self.assertRaises(ValueError):
                runner.record([], root, 10)
            with self.assertRaises(ValueError):
                runner.record([sys.executable, "-c", "pass"], root, 0)
            self.assertFalse(root.exists())

    def test_runner_cli_timeout_marks_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "scripts/record_run.py"),
                    "--out",
                    str(output),
                    "--timeout",
                    "0.2",
                    "--",
                    sys.executable,
                    "-c",
                    "import time;time.sleep(30)",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 124, result.stderr)
            saved = json.loads((output / "run.json").read_text())
            self.assertTrue(saved["timed_out"])
            self.assertFalse(saved["success"])

    def test_runner_cli_invalid_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "scripts/record_run.py"),
                    "--out",
                    str(output),
                    "--timeout",
                    "0",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_runner_cli_marks_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "scripts/record_run.py"),
                    "--out",
                    str(output),
                    "--timeout",
                    "10",
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(3)",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(
                json.loads((output / "run.json").read_text())["returncode"], 3
            )


if __name__ == "__main__":
    if "--result" in sys.argv:
        index = sys.argv.index("--result")
        result_path = Path(sys.argv[index + 1])
        del sys.argv[index : index + 2]
    else:
        result_path = None
    program = unittest.main(exit=False)
    result = program.result
    if result_path:
        with result_path.open("x", encoding="utf-8") as output:
            json.dump(
                {
                    "success": result.wasSuccessful(),
                    "tests_run": result.testsRun,
                    "failures": len(result.failures),
                    "errors": len(result.errors),
                    "skipped": len(result.skipped),
                },
                output,
                indent=2,
            )
    raise SystemExit(0 if result.wasSuccessful() else 1)
