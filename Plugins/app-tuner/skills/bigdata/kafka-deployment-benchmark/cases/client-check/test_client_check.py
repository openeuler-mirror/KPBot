"""Offline tests of the native Kafka produce/consume validation flow."""

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
spec = importlib.util.spec_from_file_location("kafka_check", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class KafkaCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "results"
        self.args = module.parser().parse_args(
            ["--kafka-dir", "kafka", "--output", str(self.output)]
        )
        self.calls = []
        self.messages = ""
        self.bad_messages = False
        self.fail_at = None
        self.timeout_at = None

    def process(self, argv, **kwargs):
        test = self
        index = len(self.calls)
        self.calls.append(argv)

        class Process:
            pid = 12345
            returncode = 1 if index == test.fail_at else 0
            attempts = 0

            def communicate(self, input=None, timeout=None):
                self.attempts += 1
                if index == test.timeout_at and self.attempts == 1:
                    raise subprocess.TimeoutExpired("kafka", timeout)
                if "kafka-console-producer.sh" in argv[1]:
                    test.messages = input
                if "kafka-console-consumer.sh" in argv[1]:
                    return (
                        "wrong\n" if test.bad_messages else test.messages
                    ), ""
                return "", "failed" if self.returncode else ""

            def kill(self):
                self.returncode = -9

        return Process()

    def run_check(self):
        with (
            patch.object(module.subprocess, "Popen", side_effect=self.process),
            patch.object(module.os, "killpg", create=True),
        ):
            return module.check(self.args)

    def test_exact_message_round_trip(self):
        result = self.run_check()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.messages.splitlines()), 3)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(
            result, json.loads((self.output / "result.json").read_text())
        )
        self.assertFalse(any("--delete" in call for call in self.calls))

    def test_create_failure_stops_without_producing(self):
        self.fail_at = 0
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertEqual(len(self.calls), 1)

    def test_produce_failure_never_reports_delivery_success(self):
        self.fail_at = 2
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertEqual(len(self.calls), 3)

    def test_wrong_messages_fail_despite_zero_exit(self):
        self.bad_messages = True
        self.assertEqual(self.run_check()["status"], "failed")

    def test_timeout_stops_and_retains_logs(self):
        self.timeout_at = 0
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertTrue((self.output / "00.stderr").exists())
        self.assertEqual(len(self.calls), 1)

    def test_client_config_routed_to_each_native_tool(self):
        self.args.client_config = Path("auth.properties")
        self.assertEqual(self.run_check()["status"], "passed")
        self.assertIn("--command-config", self.calls[0])
        self.assertIn("--producer.config", self.calls[2])
        self.assertIn("--consumer.config", self.calls[3])

    def test_results_are_not_overwritten(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.run_check()

    def test_missing_executable_is_reported(self):
        with patch.object(
            module.subprocess,
            "Popen",
            side_effect=FileNotFoundError("bash missing"),
        ):
            self.assertEqual(module.check(self.args)["status"], "failed")

    def test_consumer_is_bounded(self):
        self.run_check()
        self.assertIn("--max-messages", self.calls[-1])
        self.assertIn("--timeout-ms", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
