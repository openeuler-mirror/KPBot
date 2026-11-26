#!/usr/bin/env python3
"""Executable tier-A contract regressions for the environment skill."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_environment as check  # noqa: E402


def ready_local():
    return {
        "probes": [
            check.probe(
                "runtime.exercised_apis",
                True,
                "ready",
                "ok",
                {"tensor_shape": [3, 224, 224]},
            )
        ],
        "versions": {"ray": "2.56.0"},
    }


def ready_resource():
    return check.probe(
        "resource.cpus", True, "ready", "ok", {"effective_cpus": 2}
    )


def cgroup_fixture(root, version, membership, mount_root):
    """Build procfs and cgroup files for nested namespace fixtures."""
    proc = root / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable: 8388608 kB\n")
    mount_root_path = Path(root / "cgroup")
    if version == "v2":
        (proc / "self/cgroup").write_text(f"0::{membership}\n")
        (proc / "self/mountinfo").write_text(
            f"31 24 0:27 {mount_root} {mount_root_path} rw "
            "- cgroup2 cgroup2 rw\n"
        )
        current = mount_root_path / membership[len(mount_root):].lstrip("/")
        paths = {"current": current, "root": mount_root_path}
        if mount_root == "/" and membership != "/":
            paths["parent"] = current.parent
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
            (path / "cpu.max").write_text("max 100000\n")
            (path / "memory.max").write_text("max\n")
            (path / "memory.current").write_text("0\n")
    else:
        (proc / "self/cgroup").write_text(
            f"4:cpu,cpuacct:{membership}\n5:memory:{membership}\n"
        )
        cpu_mount = mount_root_path / "cpu"
        memory_mount = mount_root_path / "memory"
        (proc / "self/mountinfo").write_text(
            f"32 24 0:28 {mount_root} {cpu_mount} rw "
            "- cgroup cgroup rw,cpu,cpuacct\n"
            f"33 24 0:29 {mount_root} {memory_mount} rw "
            "- cgroup cgroup rw,memory\n"
        )
        relative = membership[len(mount_root):].lstrip("/")
        current = {"cpu": cpu_mount / relative, "memory": memory_mount / relative}
        paths = {
            "current": current,
            "root": {"cpu": cpu_mount, "memory": memory_mount},
        }
        if mount_root == "/" and membership != "/":
            paths["parent"] = {
                "cpu": current["cpu"].parent,
                "memory": current["memory"].parent,
            }
        for group in paths.values():
            selected = group.values() if isinstance(group, dict) else [group]
            for path in selected:
                path.mkdir(parents=True, exist_ok=True)
                (path / "cpu.cfs_quota_us").write_text("-1\n")
                (path / "cpu.cfs_period_us").write_text("100000\n")
                (path / "memory.limit_in_bytes").write_text(str(1 << 62) + "\n")
                (path / "memory.usage_in_bytes").write_text("0\n")
    return proc, paths


class EnvironmentContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kptools-ray-env-contract-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self, name="report.json", address=None):
        output = self.root / name
        argv = ["--output", str(output), "--timeout", "0.2"]
        if address:
            argv.extend(["--address", address])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = check.main(argv)
        document = None
        if output.exists():
            document = json.loads(output.read_text(encoding="utf-8"))
        return code, document, output

    def test_ready_report_and_optional_warning(self):
        optional = check.probe(
            "runtime.diagnostic", False, "unknown", "worker_stderr"
        )
        with (
            mock.patch.object(
                check, "run_local_runtime", return_value=ready_local()
            ),
            mock.patch.object(
                check,
                "resource_probes",
                return_value=[ready_resource(), optional],
            ),
        ):
            code, report, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["scope"], "local-runtime")
        self.assertIn("runtime.diagnostic", report["warnings"])
        self.assertTrue(report["probes"][0]["mandatory"])

    def test_wrong_version_blocks_ready(self):
        local = ready_local()
        local["probes"][0]["status"] = "blocked"
        local["probes"][0]["reason"] = "wrong_version"
        with (
            mock.patch.object(check, "run_local_runtime", return_value=local),
            mock.patch.object(
                check, "resource_probes", return_value=[ready_resource()]
            ),
        ):
            code, report, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["probes"][0]["reason"], "wrong_version")

    def test_missing_python_is_absent_not_success(self):
        missing = self.root / "missing-python"
        result = check.run_local_runtime(str(missing), 0.2)
        self.assertEqual(result["probes"][0]["status"], "blocked")
        self.assertEqual(result["probes"][0]["reason"], "absent")

    def test_timeout_terminates_owned_process(self):
        started = time.monotonic()
        result = check.run_process(
            [sys.executable, "-c", "import time; time.sleep(5)"], 0.1
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(time.monotonic() - started, 3)

    def test_worker_filters_secrets_and_preserves_runtime_settings(self):
        names = [
            "UNRELATED_SYNTHETIC_SECRET",
            "RAY_UNRELATED_SYNTHETIC_SECRET",
            "PATH",
            "PYTHONPATH",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "RAY_TLS_CA_CERT",
        ]
        injected = {
            "UNRELATED_SYNTHETIC_SECRET": "must-not-reach-child",
            "RAY_UNRELATED_SYNTHETIC_SECRET": "must-not-reach-child",
            "PATH": os.environ["PATH"],
            "PYTHONPATH": "synthetic-runtime-path",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "RAY_TLS_CA_CERT": "synthetic-ca-path",
        }
        code = (
            "import json, os; "
            "print(json.dumps("
            f"{{name: os.environ.get(name) for name in {names!r}}}"
            "))"
        )
        with mock.patch.dict(os.environ, injected):
            parent_environment = dict(os.environ)
            outcome = check.run_process([sys.executable, "-c", code], 5)
            self.assertEqual(dict(os.environ), parent_environment)
        self.assertTrue(outcome["ok"], outcome)
        observed = json.loads(outcome["stdout"])
        self.assertIsNone(observed["UNRELATED_SYNTHETIC_SECRET"])
        self.assertIsNone(observed["RAY_UNRELATED_SYNTHETIC_SECRET"])
        self.assertEqual(observed["PATH"], injected["PATH"])
        self.assertEqual(observed["PYTHONPATH"], "synthetic-runtime-path")
        self.assertEqual(observed["OMP_NUM_THREADS"], "1")
        self.assertEqual(observed["MKL_NUM_THREADS"], "1")
        self.assertEqual(observed["RAY_TLS_CA_CERT"], "synthetic-ca-path")

    def test_existing_cluster_auth_failure_is_blocked(self):
        cluster = {
            "probes": [
                check.probe(
                    "existing_cluster",
                    True,
                    "blocked",
                    "auth",
                    {"error": "401 Unauthorized"},
                )
            ],
            "versions": {},
        }
        with (
            mock.patch.object(
                check, "run_local_runtime", return_value=ready_local()
            ),
            mock.patch.object(
                check, "resource_probes", return_value=[ready_resource()]
            ),
            mock.patch.object(
                check, "run_cluster_probe", return_value=cluster
            ),
        ):
            code, report, _ = self.run_main(address="auto")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["scope"], "existing-cluster")
        cluster_probe = report["probes"][-1]
        self.assertEqual(cluster_probe["reason"], "auth")
        self.assertTrue(cluster_probe["mandatory"])

    def test_unknown_mandatory_probe_has_distinct_exit(self):
        local = ready_local()
        local["probes"][0]["status"] = "unknown"
        local["probes"][0]["reason"] = "unknown"
        with (
            mock.patch.object(check, "run_local_runtime", return_value=local),
            mock.patch.object(
                check, "resource_probes", return_value=[ready_resource()]
            ),
        ):
            code, report, _ = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "unknown")

    def test_output_collision_preserves_sentinel(self):
        output = self.root / "existing.json"
        output.write_text("do-not-overwrite", encoding="utf-8")
        with mock.patch.object(check, "run_local_runtime") as local:
            with redirect_stderr(io.StringIO()):
                code = check.main(["--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(
            output.read_text(encoding="utf-8"), "do-not-overwrite"
        )
        local.assert_not_called()

    def test_version_local_suffix_and_wrong_version(self):
        self.assertEqual(check.base_version("2.13.0+cpu"), "2.13.0")
        self.assertNotEqual(check.base_version("2.12.9+cpu"), "2.13.0")

    def test_credentials_are_redacted_and_userinfo_rejected(self):
        with mock.patch.dict(
            os.environ, {"RAY_AUTH_TOKEN": "synthetic-secret"}
        ):
            text = check.sanitize_text(
                "token=synthetic-secret ray://user:password@example:10001"
            )
        self.assertNotIn("synthetic-secret", text)
        self.assertNotIn("user:password", text)
        with self.assertRaises(ValueError):
            check.validate_address("ray://user:password@example:10001")
        with self.assertRaises(ValueError):
            check.validate_address("local")

    def test_error_categories_are_distinct(self):
        self.assertEqual(check.classify_error("401 Unauthorized"), "auth")
        self.assertEqual(check.classify_error("No module named ray"), "absent")
        self.assertEqual(
            check.classify_error("connection refused"), "unreachable"
        )
        self.assertEqual(check.classify_error("other failure"), "error")

    def test_object_store_values_below_ray_minimum_fail_before_output(self):
        for value in (64, 74):
            with self.subTest(value=value):
                output = self.root / f"low-object-store-{value}.json"
                with (
                    mock.patch.object(check, "run_local_runtime") as local,
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    check.main(
                        [
                            "--output",
                            str(output),
                            "--object-store-mib",
                            str(value),
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(output.exists())
                local.assert_not_called()
        args = check.build_parser().parse_args(
            ["--output", "unused", "--object-store-mib", "75"]
        )
        self.assertEqual(args.object_store_mib, 75)

    def test_shared_memory_is_mandatory_only_for_local_startup(self):
        for address in (None, "auto"):
            with self.subTest(address=address):
                output = self.root / ("local" if address is None else "remote")
                argv = ["--output", str(output), "--object-store-mib", "75"]
                if address:
                    argv.extend(["--address", address])
                args = check.build_parser().parse_args(argv)
                usage = mock.Mock(total=64 * 1024**2, free=64 * 1024**2)
                with (
                    mock.patch.object(check, "effective_cpus", return_value=(8, {})),
                    mock.patch.object(
                        check, "memory_available_bytes", return_value=(1 << 32, {})
                    ),
                    mock.patch.object(check.shutil, "disk_usage", return_value=usage),
                ):
                    probes = check.resource_probes(args)
                shared = [
                    item for item in probes if item["name"] == "resource.shared_memory"
                ]
                if address:
                    self.assertEqual(shared, [])
                else:
                    self.assertEqual(shared[0]["status"], "blocked")
                    self.assertTrue(shared[0]["mandatory"])

    def test_nested_cgroup_limits_apply_to_cpu_and_memory(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                root = self.root / f"nested-{version}"
                proc, paths = cgroup_fixture(root, version, "/parent/child", "/")
                parent = paths["parent"]
                selected = parent.values() if isinstance(parent, dict) else [parent]
                for path in selected:
                    if version == "v2":
                        (path / "cpu.max").write_text("100000 100000\n")
                        (path / "memory.max").write_text(str(1024**3) + "\n")
                        (path / "memory.current").write_text(
                            str(128 * 1024**2) + "\n"
                        )
                    else:
                        (path / "cpu.cfs_quota_us").write_text("100000\n")
                        (path / "memory.limit_in_bytes").write_text(
                            str(1024**3) + "\n"
                        )
                        (path / "memory.usage_in_bytes").write_text(
                            str(128 * 1024**2) + "\n"
                        )
                with mock.patch.object(
                    check.os, "sched_getaffinity", return_value=set(range(8))
                ):
                    cpus, _ = check.effective_cpus(proc)
                available, _ = check.memory_available_bytes(proc)
                self.assertEqual(cpus, 1)
                self.assertEqual(available, 896 * 1024**2)

    def test_namespaced_cgroup_mount_root_limit_is_not_ignored(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                root = self.root / f"root-limited-{version}"
                proc, paths = cgroup_fixture(root, version, "/limited", "/limited")
                selected = (
                    paths["root"].values()
                    if version == "v1"
                    else [paths["root"]]
                )
                for path in selected:
                    if version == "v2":
                        (path / "cpu.max").write_text("200000 100000\n")
                        (path / "memory.max").write_text(str(512 * 1024**2) + "\n")
                    else:
                        (path / "cpu.cfs_quota_us").write_text("200000\n")
                        (path / "memory.limit_in_bytes").write_text(
                            str(512 * 1024**2) + "\n"
                        )
                with mock.patch.object(
                    check.os, "sched_getaffinity", return_value=set(range(8))
                ):
                    cpus, _ = check.effective_cpus(proc)
                available, _ = check.memory_available_bytes(proc)
                self.assertEqual(cpus, 2)
                self.assertEqual(available, 512 * 1024**2)

    def test_cluster_worker_rejects_mismatched_preprocessing_packages(self):
        versions = {"python": "3.11.9", **check.EXPECTED_VERSIONS}
        self.assertEqual(check.runtime_version_mismatches(versions), [])
        versions["torchvision"] = "0.27.0"
        versions["Pillow"] = "12.0.0"
        self.assertEqual(
            check.runtime_version_mismatches(versions), ["torchvision", "Pillow"]
        )
        evidence = {"versions": versions, "errors": {}, "marker": 28}
        fake_ray = mock.Mock()
        fake_ray.remote.side_effect = lambda function: mock.Mock(
            remote=lambda: evidence
        )
        fake_ray.get.side_effect = lambda value, timeout: value
        fake_ray.cluster_resources.return_value = {"CPU": 2}
        environment = {
            "KPBOT_RAY_PROBE_ADDRESS": "auto",
            "KPBOT_RAY_PROBE_TIMEOUT": "1",
            "KPBOT_RAY_PROBE_CPUS": "2",
        }
        output = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"ray": fake_ray}),
            mock.patch.dict(os.environ, environment),
            redirect_stdout(output),
        ):
            code = check.cluster_worker()
        report = json.loads(output.getvalue())
        cluster = report["probes"][0]
        self.assertEqual(code, 0)
        self.assertEqual(cluster["status"], "blocked")
        self.assertEqual(cluster["reason"], "wrong_version")
        self.assertEqual(
            cluster["evidence"]["worker_version_mismatches"],
            ["torchvision", "Pillow"],
        )
        self.assertEqual(report["versions"]["cluster_worker"], versions)
        fake_ray.shutdown.assert_called_once_with()

    def test_eval_and_skill_contract(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: kptools-ray-imagenet-environment-check", skill)
        self.assertIn("只读", skill.split("---", 2)[1])
        evals = json.loads(
            (SKILL_DIR / "evals/evals.json").read_text(encoding="utf-8")
        )
        queries = json.loads(
            (SKILL_DIR / "evals/eval_queries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            evals["skill_name"], "kptools-ray-imagenet-environment-check"
        )
        self.assertGreaterEqual(len(evals["evals"]), 4)
        self.assertTrue(
            any(item["should_trigger"] for item in queries["queries"])
        )
        self.assertTrue(
            any(not item["should_trigger"] for item in queries["queries"])
        )
        for path in SKILL_DIR.rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".md",
                ".json",
                ".yaml",
            }:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("kpbot-ray-" + "imagenet-setup", text)
                self.assertNotIn("/" + "tmp/", text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        EnvironmentContractTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    document = {
        "success": result.wasSuccessful(),
        "checks_ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "evidence": "tier-A mocks/subprocess contracts; not a real Ray E2E",
    }
    try:
        check.write_json_exclusive(args.result, document)
    except FileExistsError:
        print(f"result already exists: {args.result}", file=sys.stderr)
        return 2
    print(json.dumps(document))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
