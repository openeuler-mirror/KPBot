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
