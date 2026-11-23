"""Offline tests of service-specific CLI checks, without a server."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_service.py"
spec = importlib.util.spec_from_file_location("service_check", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ClientCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "results"
        self.args = module.parser().parse_args(["--output", str(self.output)])
        self.calls = []

    def client(self, argv, **kwargs):
        self.calls.append(argv)
        query = argv[-1]
        values = {
            "SELECT 1": "1\n",
            "SELECT version()": "test-version\n",
        }
        value = values[query] if query in values else "499500\n"
        return subprocess.CompletedProcess(argv, 0, value, "")

    def test_complete_validation(self):
        with patch.object(module.subprocess, "run", side_effect=self.client):
            result = module.check(self.args)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result, json.loads((self.output / "result.json").read_text())
        )
        self.assertTrue((self.output / "00.stdout").exists())
        self.assertTrue(
            all(item["returncode"] == 0 for item in result["checks"])
        )

    def test_connection_failure_stops_following_queries(self):
        failed = subprocess.CompletedProcess([], 1, "", "connection refused")
        with patch.object(
            module.subprocess, "run", return_value=failed
        ) as invoke:
            result = module.check(self.args)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(
            (self.output / "00.stderr").read_text(), "connection refused"
        )

    def test_wrong_aggregate_fails_even_on_zero_exit(self):
        def wrong(argv, **kwargs):
            value = self.client(argv, **kwargs)
            if "SUM" in argv[-1].upper():
                value.stdout = "0\n"
            return value

        with patch.object(module.subprocess, "run", side_effect=wrong):
            self.assertEqual(module.check(self.args)["status"], "failed")

    def test_timeout_saves_failed_result(self):
        with patch.object(
            module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(
                "client", 1, output=b"partial"
            ),
        ):
            result = module.check(self.args)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            (self.output / "timeout.stdout").read_bytes(), b"partial"
        )

    def test_missing_client_is_reported(self):
        with patch.object(
            module.subprocess,
            "run",
            side_effect=FileNotFoundError("client not installed"),
        ):
            result = module.check(self.args)
        self.assertEqual(result["status"], "failed")
        self.assertTrue((self.output / "result.json").exists())

    def test_old_evidence_is_not_overwritten(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            module.check(self.args)

    def test_connection_options_are_passed_as_argv(self):
        self.args.host = "db-host"
        self.args.port = 19000
        self.args.config = Path("client.conf")
        with patch.object(module.subprocess, "run", side_effect=self.client):
            module.check(self.args)
        self.assertIn("db-host", self.calls[0])
        self.assertIn("19000", self.calls[0])
        self.assertIn("--config-file", self.calls[0])
        self.assertIn("client.conf", self.calls[0])

    def test_passed_result_explicitly_limits_scope(self):
        with patch.object(module.subprocess, "run", side_effect=self.client):
            result = module.check(self.args)
        self.assertEqual(result["scope"], "connection-and-sql-only")
        self.assertEqual(
            self.calls[-1][-1], "SELECT sum(number) FROM numbers(1000)"
        )
        self.assertEqual(len(self.calls), 3)

    def test_timeout_must_be_finite_and_positive(self):
        for value in ("nan", "inf", "-1", "0"):
            with self.assertRaises(Exception):
                module.positive_timeout(value)

    def test_client_layout_is_usable(self):
        self.args.client = "./clickhouse"
        self.args.single_binary = True
        with patch.object(module.subprocess, "run", side_effect=self.client):
            self.assertEqual(module.check(self.args)["status"], "passed")
        self.assertEqual(self.calls[0][:2], ["./clickhouse", "client"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
