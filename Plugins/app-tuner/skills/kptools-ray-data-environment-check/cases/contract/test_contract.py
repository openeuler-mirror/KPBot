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

    def test_unreadable_metadata_is_unknown_without_invalidmetadata(self):
        errors = (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid metadata"),
            OSError("metadata I/O failed"),
        )
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    MODULE.importlib.metadata, "version", side_effect=error
                ),
            ):
                result = MODULE.package_probe("ray")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "metadata_unreadable")
                self.assertIsNone(result["evidence"]["actual"])
                self.assertEqual(
                    result["evidence"]["error_type"], type(error).__name__
                )

    def test_nested_v2_cgroup_limits_include_only_visible_ancestors(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                MODULE.os, "sched_getaffinity", return_value=set(range(8))
            ),
            mock.patch.object(MODULE.os, "cpu_count", return_value=8),
        ):
            root = Path(directory)
            nested_v2_fixture(root)
            cpus = MODULE.visible_cpus(root)
            memory = MODULE.memory_probe(root)
        self.assertEqual(cpus["status"], "blocked")
        self.assertEqual(cpus["evidence"]["cgroup_quotas"], [1.0, 4.0])
        self.assertEqual(cpus["evidence"]["visible_upper_bound"], 1.0)
        self.assertEqual(memory["status"], "blocked")
        self.assertEqual(
            memory["evidence"]["cgroup_remaining_by_group_bytes"],
            [128 * 1024 * 1024, 1024 * 1024 * 1024],
        )
        self.assertEqual(
            memory["evidence"]["available_upper_bound_bytes"],
            128 * 1024 * 1024,
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
            write(root / "proc/meminfo", "MemAvailable: 8388608 kB\n")
            write(root / "sys/fs/cgroup/cpu/cpu.cfs_quota_us", "100000")
            write(root / "sys/fs/cgroup/cpu/cpu.cfs_period_us", "100000")
            write(root / "sys/fs/cgroup/memory/memory.limit_in_bytes", "134217728")
            write(root / "sys/fs/cgroup/memory/memory.usage_in_bytes", "0")
            with (
                mock.patch.object(
                    MODULE.os, "sched_getaffinity", return_value=set(range(8))
                ),
                mock.patch.object(MODULE.os, "cpu_count", return_value=8),
            ):
                cpus = MODULE.visible_cpus(root)
            memory = MODULE.memory_probe(root)
        self.assertEqual(cpus["reason"], "insufficient_cpus")
        self.assertEqual(cpus["evidence"]["cgroup_quota"], 1.0)
        self.assertEqual(memory["reason"], "insufficient_available_memory")
        self.assertEqual(
            memory["evidence"]["cgroup_remaining_bytes"], 128 * 1024 * 1024
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
