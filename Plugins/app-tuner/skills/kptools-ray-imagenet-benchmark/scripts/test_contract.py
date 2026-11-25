#!/usr/bin/env python3
"""Executable tier-A contract regressions for the benchmark skill."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_support as support  # noqa: E402
import benchmark as bench  # noqa: E402


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


def valid_versions():
    versions = {
        name: value for name, value in support.EXPECTED_VERSIONS.items()
    }
    versions["python"] = "3.11.9"
    versions["torch"] = "2.13.0+cpu"
    versions["torchvision"] = "0.28.0+cpu"
    return versions


def valid_round(index, elapsed=100.0):
    return {
        "kind": "timed",
        "index": index,
        "status": "passed",
        "correct": True,
        "output_rows": 8,
        "wall_elapsed_ms": elapsed,
        "throughput_images_per_second": 8000.0 / elapsed,
        "transform_latency_ms": {"min": 1.0, "median": 2.0, "max": 3.0},
    }


def valid_document():
    warmup = valid_round(0, 99999.0)
    warmup["kind"] = "warmup"
    timed = [valid_round(index, 100.0 + index * 50) for index in range(3)]
    return {
        "status": "passed",
        "success": True,
        "correctness": True,
        "warmup_rounds": [warmup],
        "timed_rounds": timed,
        "summary": bench.summarize_rounds(timed, [warmup]),
    }


def verify_e2e_result(result_path):
    """Independently verify a real result and all linked raw evidence."""
    root = result_path.parent
    document = json.loads(result_path.read_text(encoding="utf-8"))
    if not document.get("success") or not document.get("correctness"):
        raise ValueError("real result is not successful and correct")
    if document.get("is_full_imagenet_benchmark") is not False:
        raise ValueError("real result has an invalid workload scope")
    if document.get("full_imagenet_accuracy") is not None:
        raise ValueError("smoke unexpectedly claims ImageNet accuracy")
    timed = document["timed_rounds"]
    warmup = document["warmup_rounds"]
    if len(timed) < 3 or not warmup:
        raise ValueError("real result lacks warmup or three timed rounds")
    expected = json.loads(
        (root / "serial-reference.json").read_text(encoding="utf-8")
    )
    fixture = document["fixture"]
    if fixture.get("is_imagenet") is not False:
        raise ValueError("fixture is not declared synthetic")
    for item in fixture["files"]:
        if Path(item["id"]).name != item["id"]:
            raise ValueError("unsafe fixture identifier")
        content = (root / "fixture" / item["id"]).read_bytes()
        if len(content) != item["bytes"]:
            raise ValueError(f"fixture byte count differs: {item['id']}")
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError(f"fixture digest differs: {item['id']}")
    for item in warmup + timed:
        if item["status"] != "passed" or not item["correct"]:
            raise ValueError("real result contains a failed round")
        if item["output_rows"] != len(expected):
            raise ValueError("real result output count differs from reference")
        worker_profiles = item.get("preprocessing_worker_versions")
        if not worker_profiles:
            raise ValueError("real result lacks preprocessing worker versions")
        for key in ("raw_rows_file", "native_dataset_stats_file"):
            if Path(item[key]).name != item[key]:
                raise ValueError("unsafe evidence path")
        rows = json.loads((root / item["raw_rows_file"]).read_text())
        actual = {row["id"]: row["tensor_sha256"] for row in rows}
        if len(rows) != len(actual) or actual != expected:
            raise ValueError("raw Ray rows differ from serial reference")
        if any(
            row.get("worker_versions") not in worker_profiles for row in rows
        ):
            raise ValueError("raw rows omit recorded preprocessing versions")
        stats = (root / item["native_dataset_stats_file"]).read_text()
        if item["native_dataset_stats"] != stats:
            raise ValueError("inline/native stats differ")
        if (
            "Dataset throughput:" not in stats
            or "Total output num rows:" not in stats
        ):
            raise ValueError("native Ray metrics are incomplete")
    summary = document["summary"]
    samples = [item["wall_elapsed_ms"] for item in timed]
    if summary["wall_elapsed_ms"]["samples"] != samples:
        raise ValueError("summary changed raw timed samples")
    if not summary["warmup_excluded"]:
        raise ValueError("warmup was not excluded")
    if summary["timed_round_count"] != len(timed):
        raise ValueError("summary timed count differs")
    if summary["total_timed_images"] != len(timed) * len(expected):
        raise ValueError("summary image count differs")
    return {
        "timed_rounds": len(timed),
        "warmup_rounds": len(warmup),
        "verified_rows_per_round": len(expected),
        "native_stats_verified": True,
        "fixture_hashes_verified": True,
        "warmup_excluded": True,
    }


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kptools-ray-bench-contract-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_summary_excludes_warmup_samples(self):
        warmup = valid_round(0, 99999.0)
        timed = [valid_round(index, 100.0 + index * 100) for index in range(3)]
        summary = bench.summarize_rounds(timed, [warmup])
        self.assertEqual(
            summary["wall_elapsed_ms"]["samples"], [100, 200, 300]
        )
        self.assertEqual(summary["wall_elapsed_ms"]["median"], 200)
        self.assertEqual(summary["warmup_round_count_excluded"], 1)
        self.assertTrue(summary["warmup_excluded"])
        self.assertEqual(summary["total_timed_images"], 24)

    def test_exact_correctness_accepts_matching_post_batch_digest(self):
        rows = [
            {
                "id": "a",
                "tensor_sha256": "digest-a",
                "transform_latency_ms": 1.0,
                "actor_batch_latency_ms": 2.0,
            }
        ]
        result = bench.verify_rows({"a": "digest-a"}, rows)
        self.assertTrue(result["correct"])
        self.assertEqual(result["unique_output_ids"], 1)

    def test_exact_correctness_rejects_bad_missing_and_duplicate_rows(self):
        base = {
            "id": "a",
            "tensor_sha256": "digest-a",
            "transform_latency_ms": 1.0,
            "actor_batch_latency_ms": 2.0,
        }
        with self.subTest("digest"):
            with self.assertRaises(bench.CorrectnessError):
                bench.verify_rows({"a": "expected"}, [base])
        with self.subTest("missing"):
            with self.assertRaises(bench.CorrectnessError):
                bench.verify_rows({"a": "digest-a", "b": "digest-b"}, [base])
        with self.subTest("duplicate"):
            with self.assertRaises(bench.CorrectnessError):
                bench.verify_rows({"a": "digest-a"}, [base, dict(base)])

    def test_unsupported_and_missing_versions_fail_closed(self):
        self.assertEqual(
            support.validate_version_profile(valid_versions()), []
        )
        wrong = valid_versions()
        wrong["ray"] = "2.55.0"
        self.assertTrue(
            any(
                "ray" in item
                for item in support.validate_version_profile(wrong)
            )
        )
        missing = valid_versions()
        del missing["Pillow"]
        self.assertTrue(
            any(
                "Pillow" in item
                for item in support.validate_version_profile(missing)
            )
        )

    def test_auth_and_absence_have_distinct_categories(self):
        self.assertEqual(support.classify_error("401 Unauthorized"), "auth")
        self.assertEqual(
            support.classify_error("No module named ray"), "absent"
        )

    def test_supervisor_success_requires_semantic_contract(self):
        document = valid_document()
        code_text = (
            "import json, pathlib, sys; "
            "pathlib.Path(sys.argv[1], 'child-result.json').write_text("
            "json.dumps(json.loads(sys.argv[2])), encoding='utf-8')"
        )
        output = self.root / "success"
        command = [
            sys.executable,
            "-c",
            code_text,
            str(output),
            json.dumps(document),
        ]
        code, result = bench.supervise(
            command,
            output,
            5,
            validator=lambda item: bench.validate_success_document(item, 3, 1),
        )
        self.assertEqual(code, 0)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["timed_rounds"]), 3)
        self.assertEqual(
            json.loads((output / "result.json").read_text())["summary"],
            result["summary"],
        )

    def test_supervisor_filters_secrets_and_preserves_runtime_settings(self):
        document = valid_document()
        names = [
            "UNRELATED_SYNTHETIC_SECRET",
            "RAY_UNRELATED_SYNTHETIC_SECRET",
            "PATH",
            "PYTHONPATH",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
            "RAY_TLS_CA_CERT",
        ]
        code_text = (
            "import json, os, pathlib, sys; "
            "document = json.loads(sys.argv[2]); "
            "document['observed_child_env'] = "
            f"{{name: os.environ.get(name) for name in {names!r}}}; "
            "pathlib.Path(sys.argv[1], 'child-result.json').write_text("
            "json.dumps(document), encoding='utf-8')"
        )
        output = self.root / "filtered-environment"
        command = [
            sys.executable,
            "-c",
            code_text,
            str(output),
            json.dumps(document),
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
        with mock.patch.dict(os.environ, injected):
            parent_environment = dict(os.environ)
            code, result = bench.supervise(command, output, 5)
            self.assertEqual(dict(os.environ), parent_environment)
        self.assertEqual(code, 0)
        observed = result["observed_child_env"]
        self.assertIsNone(observed["UNRELATED_SYNTHETIC_SECRET"])
        self.assertIsNone(observed["RAY_UNRELATED_SYNTHETIC_SECRET"])
        self.assertEqual(observed["PATH"], injected["PATH"])
        self.assertEqual(observed["PYTHONPATH"], "synthetic-runtime-path")
        self.assertEqual(observed["OMP_NUM_THREADS"], "1")
        self.assertEqual(observed["MKL_NUM_THREADS"], "1")
        self.assertEqual(observed["RAY_TLS_CA_CERT"], "synthetic-ca-path")

    def test_supervisor_rejects_false_success(self):
        document = valid_document()
        document["timed_rounds"] = []
        code_text = (
            "import json, pathlib, sys; "
            "pathlib.Path(sys.argv[1], 'child-result.json').write_text("
            "json.dumps(json.loads(sys.argv[2])), encoding='utf-8')"
        )
        output = self.root / "false-success"
        command = [
            sys.executable,
            "-c",
            code_text,
            str(output),
            json.dumps(document),
        ]
        code, result = bench.supervise(
            command,
            output,
            5,
            validator=lambda item: bench.validate_success_document(item, 3, 1),
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_category"], "contract")

    def test_timeout_preserves_structured_failure_and_logs(self):
        output = self.root / "timeout"
        code, result = bench.supervise(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            output,
            0.1,
        )
        self.assertEqual(code, 124)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_category"], "timeout")
        self.assertTrue(result["supervisor"]["timed_out"])
        self.assertTrue((output / "stderr.log").exists())

    def test_runtime_failure_preserves_stderr(self):
        output = self.root / "runtime-failure"
        code, result = bench.supervise(
            [
                sys.executable,
                "-c",
                "import sys; "
                "print('synthetic-runtime-failure', file=sys.stderr); "
                "raise SystemExit(7)",
            ],
            output,
            5,
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["success"])
        self.assertEqual(result["supervisor"]["returncode"], 7)
        self.assertIn(
            "synthetic-runtime-failure",
            (output / "stderr.log").read_text(encoding="utf-8"),
        )

    def test_missing_executable_never_reports_success(self):
        output = self.root / "missing-executable"
        code, result = bench.supervise(
            [str(self.root / "not-installed")], output, 1
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["success"])
        self.assertIn("startup_error", result["supervisor"])

    def test_exclusive_writer_creates_parents_and_never_overwrites(self):
        output = self.root / "new" / "nested" / "result.json"
        support.write_json_exclusive(output, {"success": True})
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")), {"success": True}
        )
        with self.assertRaises(FileExistsError):
            support.write_json_exclusive(output, {"success": False})
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8")), {"success": True}
        )

    def test_output_collision_preserves_existing_tree(self):
        output = self.root / "collision"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = bench.main(["--output", str(output)])
        self.assertEqual(code, 2)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_iteration_and_timeout_bounds(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            bench.finite_timeout("inf")
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            bench.build_parser().parse_args(
                ["--output", "unused", "--rounds", "2"]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_credentials_and_endpoint_userinfo_are_not_accepted(self):
        with mock.patch.dict(
            os.environ, {"RAY_AUTH_TOKEN": "synthetic-secret"}
        ):
            self.assertNotIn(
                "synthetic-secret",
                support.sanitize_text("RAY_AUTH_TOKEN=synthetic-secret"),
            )
        with self.assertRaises(ValueError):
            bench.validate_address("ray://user:password@example:10001")
        with self.assertRaises(ValueError):
            bench.validate_address("local")

    def test_object_store_values_below_ray_minimum_fail_before_output(self):
        for value in (64, 74):
            with self.subTest(value=value):
                output = self.root / f"low-object-store-{value}"
                with (
                    mock.patch.object(bench, "supervise") as supervisor,
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    bench.main(
                        [
                            "--output",
                            str(output),
                            "--object-store-mib",
                            str(value),
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(output.exists())
                supervisor.assert_not_called()
        args = bench.build_parser().parse_args(
            ["--output", "unused", "--object-store-mib", "75"]
        )
        self.assertEqual(args.object_store_mib, 75)

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
                    support.os, "sched_getaffinity", return_value=set(range(8))
                ):
                    cpus, _ = support.effective_cpus(proc)
                available, _ = support.memory_available_bytes(proc)
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
                    support.os, "sched_getaffinity", return_value=set(range(8))
                ):
                    cpus, _ = support.effective_cpus(proc)
                available, _ = support.memory_available_bytes(proc)
                self.assertEqual(cpus, 2)
                self.assertEqual(available, 512 * 1024**2)

    def test_preprocessing_worker_versions_fail_closed(self):
        row = {"worker_versions": valid_versions()}
        self.assertEqual(bench.verify_worker_versions([row]), [valid_versions()])
        for name, value in (("torchvision", "0.27.0"), ("Pillow", "12.0.0")):
            with self.subTest(name=name):
                invalid = valid_versions()
                invalid[name] = value
                with self.assertRaises(bench.CorrectnessError):
                    bench.verify_worker_versions([{"worker_versions": invalid}])
        with self.assertRaises(bench.CorrectnessError):
            bench.verify_worker_versions([{}])

    def test_failed_round_preserves_take_all_elapsed_time(self):
        class FakeDataset:
            def map(self, *args, **kwargs):
                return self

            def map_batches(self, *args, **kwargs):
                return self

            def take_all(self):
                return [
                    {
                        "id": "a",
                        "tensor_sha256": "wrong",
                        "transform_latency_ms": 1.0,
                        "actor_batch_latency_ms": 2.0,
                        "worker_versions": valid_versions(),
                    }
                ]

            def stats(self):
                return "synthetic native stats"

        class FakeData:
            @staticmethod
            def from_items(*args, **kwargs):
                return FakeDataset()

        class FakeRay:
            data = FakeData()

        for failure in ("evidence", "digest"):
            with self.subTest(failure=failure):
                output = self.root / failure
                output.mkdir()
                args = argparse.Namespace(output=output, batch_size=1)
                patches = [
                    mock.patch.object(
                        bench.time,
                        "perf_counter",
                        side_effect=[10.0, 10.25, 90.0],
                    ),
                    mock.patch.dict(
                        sys.modules,
                        {
                            "ray": mock.Mock(data=FakeData()),
                            "ray.data": mock.Mock(ActorPoolStrategy=mock.Mock()),
                        },
                    ),
                ]
                if failure == "evidence":
                    patches.append(
                        mock.patch.object(
                            bench.support,
                            "write_text_exclusive",
                            side_effect=OSError("write failed"),
                        )
                    )
                for patch in patches:
                    patch.start()
                try:
                    result = bench.execute_round(
                        FakeRay(), [], {"a": "expected"}, args, "timed", 0
                    )
                finally:
                    for patch in reversed(patches):
                        patch.stop()
                self.assertEqual(result["wall_elapsed_ms"], 250.0)
                self.assertEqual(result["status"], "failed")

    def test_eval_and_skill_contract(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: kptools-ray-imagenet-benchmark", skill)
        self.assertIn("基准", skill.split("---", 2)[1])
        evals = json.loads(
            (SKILL_DIR / "evals/evals.json").read_text(encoding="utf-8")
        )
        queries = json.loads(
            (SKILL_DIR / "evals/eval_queries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(evals["skill_name"], "kptools-ray-imagenet-benchmark")
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
        script = (SKILL_DIR / "scripts/benchmark.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"ray", "stop"', script)
        self.assertNotIn("ray stop", script.lower())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--e2e-result", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        BenchmarkContractTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    e2e_verification = None
    e2e_error = None
    if args.e2e_result:
        try:
            e2e_verification = verify_e2e_result(args.e2e_result)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            e2e_error = f"{type(exc).__name__}: {exc}"
    success = result.wasSuccessful() and e2e_error is None
    document = {
        "success": success,
        "checks_ran": result.testsRun + (1 if args.e2e_result else 0),
        "failures": len(result.failures) + (1 if e2e_error else 0),
        "errors": len(result.errors),
        "evidence": (
            "tier-A contracts plus independent raw E2E evidence verification"
            if args.e2e_result
            else "tier-A contracts/subprocesses; not a real Ray E2E"
        ),
    }
    if args.e2e_result:
        document["e2e_verification"] = e2e_verification
        document["e2e_error"] = e2e_error
    try:
        support.write_json_exclusive(args.result, document)
    except FileExistsError:
        print(f"result already exists: {args.result}", file=sys.stderr)
        return 2
    print(json.dumps(document))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
