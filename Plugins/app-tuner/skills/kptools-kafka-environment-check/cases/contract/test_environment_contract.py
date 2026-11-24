#!/usr/bin/env python3
"""Offline contract tests; these mocks are not real-service E2E evidence."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
import check_environment as check  # noqa: E402
import kafka_support as support  # noqa: E402


class EnvironmentContractTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "results" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.kafka = self.root / "kafka"
        (self.kafka / "bin").mkdir(parents=True)
        for tool in check.REQUIRED_TOOLS:
            path = self.kafka / "bin" / tool
            path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            path.chmod(0o755)
        self.output = self.root / "nested" / "evidence"
        self.calls = []
        self.mode = "success"
        self.secret = "contract-secret-do-not-disclose"

    def arguments(self, authorized=True, create=False):
        arguments = [
            "--kafka-dir",
            str(self.kafka),
            "--output",
            str(self.output),
            "--records",
            "100",
            "--warmup-records",
            "10",
            "--timeout",
            "5",
        ]
        if create:
            arguments.append("--allow-create-topic")
        else:
            arguments.extend(("--topic", "kpbot-contract"))
        if authorized:
            arguments.append("--operations-authorized")
        return arguments

    def response(self, argv):
        if Path(argv[0]).name == "java" and argv[1:] == ["-version"]:
            version = "11.0.31" if self.mode == "old-java" else "17.0.13"
            return {
                "returncode": 0,
                "stderr": f'openjdk version "{version}"\n',
            }
        parameters = argv[2:]
        if "--version" in parameters:
            version = "3.9.1" if self.mode == "old-kafka" else "4.1.0"
            return {"returncode": 0, "stdout": version + " (Commit: test)\n"}
        if "--list" in parameters:
            if self.mode == "auth":
                return {
                    "returncode": 1,
                    "stderr": (
                        "SaslAuthenticationException password=" + self.secret
                    ),
                }
            if self.mode == "timeout":
                return {"returncode": -9, "timed_out": True}
            if self.mode == "runtime":
                return {"returncode": 7, "stderr": "synthetic runtime failure"}
            return {"returncode": 0, "stdout": ""}
        if Path(argv[1]).name == "kafka-get-offsets.sh":
            latest = 5 if self.mode == "nonempty" else 0
            topic = parameters[parameters.index("--topic") + 1]
            return {"returncode": 0, "stdout": f"{topic}:0:{latest}\n"}
        if "--describe" in parameters:
            return {
                "returncode": 0,
                "stdout": (
                    "Topic: kpbot-contract PartitionCount: 1 "
                    "ReplicationFactor: 1\n"
                ),
            }
        raise AssertionError("unexpected read-only probe: " + repr(argv))

    def fake_run_process(
        self,
        argv,
        timeout,
        input_text=None,
        redactions=(),
    ):
        self.calls.append(list(argv))
        response = {
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "timed_out": False,
            "drain_timed_out": False,
            "error_category": None,
            **self.response(argv),
        }
        return {
            "command": [support.redact(item, redactions) for item in argv],
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "duration_ms": 1.0,
            "stdout": support.redact(response["stdout"], redactions),
            "stderr": support.redact(response["stderr"], redactions),
            **{
                key: value
                for key, value in response.items()
                if key not in ("stdout", "stderr")
            },
        }

    def invoke(self, arguments=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                support, "run_process", side_effect=self.fake_run_process
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = check.main(arguments or self.arguments())
        result = None
        result_path = self.output / "result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return code, result, stdout.getvalue(), stderr.getvalue()

    def probe(self, result, name):
        return next(item for item in result["probes"] if item["name"] == name)

    def test_ready_requires_live_metadata_and_semantic_output(self):
        code, result, stdout, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(json.loads(stdout), result)
        self.assertTrue(result["read_only"])
        self.assertEqual(
            self.probe(result, "authenticated_metadata")["category"], "ok"
        )
        self.assertEqual(
            self.probe(result, "cli_version")["evidence"]["version"],
            "4.1.0",
        )
        forbidden = ("--create", "--delete", "--produce")
        self.assertFalse(
            any(flag in call for call in self.calls for flag in forbidden)
        )
        optional = self.probe(result, "host_resource_evidence")
        self.assertFalse(optional["mandatory"])

    def test_missing_operation_declaration_is_unknown(self):
        code, result, _, _ = self.invoke(self.arguments(authorized=False))
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            self.probe(result, "operation_authorization")["status"],
            "unknown",
        )

    def test_create_plan_remains_read_only(self):
        code, result, _, _ = self.invoke(self.arguments(create=True))
        self.assertEqual(code, 0)
        self.assertEqual(
            self.probe(result, "topic_plan")["evidence"][
                "create_or_delete_executed"
            ],
            False,
        )
        self.assertFalse(any("--create" in call for call in self.calls))

    def test_unsupported_kafka_version_is_blocked(self):
        self.mode = "old-kafka"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            self.probe(result, "cli_version")["category"], "wrong_version"
        )

    def test_unsupported_java_version_is_blocked(self):
        self.mode = "old-java"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "java_runtime")["category"], "wrong_version"
        )

    def test_java_home_overrides_older_path_java(self):
        java_home = self.root / "selected-jdk"
        executable = java_home / "bin/java"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        response = self.response

        def conflicting_path(argv):
            if argv[:2] == ["java", "-version"]:
                return {"returncode": 0, "stderr": 'version "11.0.31"\n'}
            return response(argv)

        self.response = conflicting_path
        with patch.dict(os.environ, {"JAVA_HOME": str(java_home)}):
            code, result, _, _ = self.invoke()
        self.assertEqual(code, 0)
        evidence = self.probe(result, "java_runtime")["evidence"]
        self.assertEqual(evidence["major"], 17)
        self.assertEqual(evidence["selected_from"], "JAVA_HOME")
        self.assertEqual(evidence["command"]["command"][0], str(executable))

    def test_missing_tool_is_distinct_from_version_failure(self):
        (self.kafka / "bin/kafka-get-offsets.sh").unlink()
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "official_cli_tools")["category"], "absent"
        )
        self.assertEqual(self.probe(result, "cli_version")["category"], "ok")

    def test_authentication_failure_is_classified(self):
        self.mode = "auth"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "authenticated_metadata")["category"], "auth"
        )

    def test_timeout_is_classified(self):
        self.mode = "timeout"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "authenticated_metadata")["category"],
            "timeout",
        )

    def test_runtime_failure_is_not_ready(self):
        self.mode = "runtime"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            self.probe(result, "authenticated_metadata")["category"], "error"
        )

    def test_nonempty_existing_topic_is_not_ready(self):
        self.mode = "nonempty"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "topic_metadata")["category"],
            "topic_not_empty",
        )
        self.assertEqual(
            self.probe(result, "topic_metadata")["evidence"][
                "latest_offset_sum"
            ],
            5,
        )

    def test_output_collision_preserves_existing_evidence(self):
        self.output.mkdir(parents=True)
        sentinel = self.output / "result.json"
        sentinel.write_text("do-not-overwrite", encoding="utf-8")
        code, result, _, stderr = self.invoke()
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "do-not-overwrite"
        )
        self.assertIn("output_collision", stderr)

    def test_secure_config_is_hashed_and_secrets_are_redacted(self):
        config = self.root / "secure.properties"
        config.write_text(
            "security.protocol=SASL_SSL\n"
            "sasl.jaas.config=org.example.Login required "
            f'password="{self.secret}";\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        self.mode = "auth"
        arguments = self.arguments() + ["--client-config", str(config)]
        code, result, stdout, _ = self.invoke(arguments)
        self.assertEqual(code, 1)
        serialized = json.dumps(result) + stdout
        self.assertNotIn(self.secret, serialized)
        evidence = self.probe(result, "secure_client_config")["evidence"]
        self.assertEqual(evidence["file_mode"], "0600")
        self.assertEqual(len(evidence["sha256"]), 64)
        self.assertFalse((self.output / config.name).exists())

    def test_insecure_config_stops_before_network(self):
        config = self.root / "insecure.properties"
        config.write_text("security.protocol=SSL\n", encoding="utf-8")
        config.chmod(0o644)
        code, result, _, _ = self.invoke(
            self.arguments() + ["--client-config", str(config)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "secure_client_config")["category"],
            "insecure_config",
        )
        self.assertEqual(self.calls, [])

    def test_business_group_config_is_rejected_before_probes(self):
        config = self.root / "group.properties"
        config.write_text("group\\\n.id=business\n", encoding="utf-8")
        config.chmod(0o600)
        code, result, _, _ = self.invoke(
            self.arguments() + ["--client-config", str(config)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            self.probe(result, "secure_client_config")["category"],
            "invalid_config",
        )
        self.assertEqual(self.calls, [])

    def test_nonfinite_or_unbounded_arguments_are_rejected(self):
        invalid = (
            ("--timeout", "inf"),
            ("--timeout", "0"),
            ("--rounds", "2"),
            ("--records", "0"),
            ("--throughput", "-1"),
        )
        for flag, value in invalid:
            with self.subTest(flag=flag, value=value):
                parser = check.build_parser()
                arguments = self.arguments()
                if flag in arguments:
                    arguments[arguments.index(flag) + 1] = value
                else:
                    arguments.extend((flag, value))
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    parser.parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_java_properties_parser_handles_escapes_and_continuations(self):
        properties = support.java_properties(
            "\\u0067roup.id=business\n"
            "sasl.jaas.config=login\\\n"
            "  required\n"
            "# group.id=comment\n"
        )
        self.assertEqual(properties["group.id"], "business")
        self.assertEqual(properties["sasl.jaas.config"], "loginrequired")

    def test_isolated_flat_copy_runs_without_sibling_or_source_tree(self):
        target = self.root / "flat" / SKILL.name
        shutil.copytree(
            SKILL,
            target,
            ignore=shutil.ignore_patterns("results", "__pycache__"),
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(target / "scripts/check_environment.py"),
                "--help",
            ],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("read-only preflight", completed.stdout.lower())
        self.assertTrue((target / "references/probes.md").is_file())
        self.assertTrue((target / "references/metrics.md").is_file())
        inspected = (target / "SKILL.md").read_text(encoding="utf-8")
        inspected += (target / "scripts/check_environment.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("kptools-kafka-benchmark", inspected)
        self.assertNotIn(".input", inspected)

    def test_keyboard_interrupt_kills_group_and_bounded_drains(self):
        process = Mock()
        process.pid = 23456
        process.communicate.side_effect = (
            KeyboardInterrupt(),
            ("", ""),
        )
        process.returncode = -9
        killpg = Mock()
        with (
            patch.object(support.subprocess, "Popen", return_value=process),
            patch.object(support.os, "killpg", killpg),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            support.run_process(["bash", "tool"], 1)
        killpg.assert_called_once_with(23456, signal.SIGKILL)
        self.assertEqual(
            process.communicate.call_args_list[1].kwargs["timeout"],
            support.DRAIN_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            raised.exception.process_result["error_category"], "interrupted"
        )

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_real_sigint_leaves_no_client_or_descendant_running(self):
        pid_file = self.root / "real-client.pids"
        client = (
            "import os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            "Path(sys.argv[1]).write_text("
            "f'{os.getpid()} {child.pid}\\n', encoding='utf-8')\n"
            "time.sleep(0.2)\n"
            "os.kill(os.getppid(), signal.SIGINT)\n"
            "child.wait()\n"
        )
        try:
            with self.assertRaises(KeyboardInterrupt) as raised:
                support.run_process(
                    [sys.executable, "-c", client, str(pid_file)], 10
                )
            interrupted = raised.exception.process_result
            self.assertEqual(interrupted["error_category"], "interrupted")
            self.assertFalse(interrupted["drain_timed_out"])
            self.assertEqual(interrupted["returncode"], -signal.SIGKILL)
            pids = [int(value) for value in pid_file.read_text().split()]
            self.assertEqual(len(pids), 2)
            deadline = time.monotonic() + support.DRAIN_TIMEOUT_SECONDS
            for pid in pids:
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    try:
                        status = Path(f"/proc/{pid}/stat").read_text(
                            encoding="utf-8"
                        )
                    except FileNotFoundError:
                        break
                    if status.rpartition(") ")[2].startswith("Z"):
                        break
                    self.assertLess(
                        time.monotonic(), deadline, f"client {pid} remains"
                    )
                    time.sleep(0.02)
        finally:
            if pid_file.exists():
                owned_group = int(pid_file.read_text().split()[0])
                try:
                    os.killpg(owned_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        EnvironmentContractTests
    )
    test_result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": test_result.wasSuccessful(),
                "tests": test_result.testsRun,
                "failures": len(test_result.failures),
                "errors": len(test_result.errors),
                "evidence_tier": "offline_contract_not_service_e2e",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.exit(0 if test_result.wasSuccessful() else 1)
