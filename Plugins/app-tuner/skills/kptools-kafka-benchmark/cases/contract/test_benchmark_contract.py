#!/usr/bin/env python3
"""Stateful offline benchmark contracts; not real-service E2E evidence."""

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
import run_benchmark as benchmark  # noqa: E402
import kafka_support as support  # noqa: E402


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent / "results" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.kafka = self.root / "kafka"
        (self.kafka / "bin").mkdir(parents=True)
        for tool in benchmark.REQUIRED_TOOLS:
            path = self.kafka / "bin" / tool
            path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            path.chmod(0o755)
        self.output = self.root / "results"
        self.calls = []
        self.offset = 0
        self.messages = ""
        self.all_messages = []
        self.mode = "success"
        self.fail_perf_at = None
        self.timeout_perf_at = None
        self.perf_count = 0
        self.created_topics = []
        self.deleted_topics = []
        self.deleted_groups = []
        self.native_count_adjustment = 0
        self.offset_adjustment = 0
        self.secret = "benchmark-secret-do-not-disclose"

    def arguments(self, create=True):
        arguments = [
            "--kafka-dir",
            str(self.kafka),
            "--output",
            str(self.output),
            "--records",
            "100",
            "--warmup-records",
            "10",
            "--throughput",
            "1000",
            "--timeout",
            "5",
            "--replication-factor",
            "1",
        ]
        if create:
            arguments.append("--allow-create-topic")
        else:
            arguments.extend(("--topic", "kpbot-existing-contract"))
        return arguments

    def perf_summary(self, records, rate):
        return (
            f"{records} records sent, {rate:.1f} records/sec "
            "(12.34 MB/sec), 4.00 ms avg latency, 19.00 ms max "
            "latency, 3 ms 50th, 8 ms 95th, 12 ms 99th, "
            "18 ms 99.9th.\n"
        )

    def response(self, argv, input_text):
        if Path(argv[0]).name == "java" and argv[1:] == ["-version"]:
            version = "11.0.31" if self.mode == "old-java" else "17.0.13"
            return {
                "returncode": 0,
                "stderr": f'openjdk version "{version}"\n',
            }
        tool = Path(argv[1]).name
        parameters = argv[2:]

        def value(flag):
            return parameters[parameters.index(flag) + 1]

        if tool == "kafka-topics.sh" and "--version" in parameters:
            version = "3.9.1" if self.mode == "old-kafka" else "4.1.0"
            return {"returncode": 0, "stdout": version + " (Commit: test)\n"}
        if tool == "kafka-topics.sh" and "--list" in parameters:
            if self.mode == "auth":
                return {
                    "returncode": 1,
                    "stderr": (
                        "SaslAuthenticationException password=" + self.secret
                    ),
                }
            if self.mode == "preflight-timeout":
                return {"returncode": -9, "timed_out": True}
            return {"returncode": 0, "stdout": ""}
        if tool == "kafka-topics.sh" and "--create" in parameters:
            topic = value("--topic")
            if self.mode == "create-timeout":
                return {"returncode": -9, "timed_out": True}
            if self.mode == "create-error":
                return {"returncode": 1, "stderr": "synthetic create error"}
            self.created_topics.append(topic)
            self.offset = 0
            return {"returncode": 0, "stdout": "Created topic " + topic}
        if tool == "kafka-topics.sh" and "--describe" in parameters:
            return {
                "returncode": 0,
                "stdout": (
                    "Topic: "
                    + value("--topic")
                    + " PartitionCount: 1 ReplicationFactor: 1\n"
                ),
            }
        if tool == "kafka-topics.sh" and "--delete" in parameters:
            topic = value("--topic")
            if self.mode == "cleanup-failure":
                return {"returncode": 9, "stderr": "synthetic cleanup error"}
            self.deleted_topics.append(topic)
            return {"returncode": 0}
        if tool == "kafka-get-offsets.sh":
            return {
                "returncode": 0,
                "stdout": f"{value('--topic')}:0:{self.offset}\n",
            }
        if tool == "kafka-console-producer.sh":
            self.messages = input_text
            self.all_messages = input_text.splitlines()
            self.offset += len(input_text.splitlines())
            return {"returncode": 0}
        if tool == "kafka-console-consumer.sh":
            consumed = list(self.all_messages)
            if self.mode == "bad-messages":
                consumed = ["wrong-message"]
            elif self.mode == "identity-mismatch" and self.perf_count == 2:
                consumed[-1] = "unexpected-identity"
            output = "\n".join(consumed) + "\n" if consumed else ""
            return {"returncode": 0, "stdout": output}
        if tool == "kafka-consumer-groups.sh":
            group = value("--group")
            if self.mode == "group-cleanup-failure":
                return {"returncode": 9, "stderr": "synthetic group error"}
            self.deleted_groups.append(group)
            return {"returncode": 0}
        if tool == "kafka-producer-perf-test.sh":
            self.perf_count += 1
            records = int(value("--num-records"))
            if self.fail_perf_at == self.perf_count:
                return {"returncode": 42, "stderr": "synthetic perf failure"}
            if self.timeout_perf_at == self.perf_count:
                return {"returncode": -9, "timed_out": True}
            native_records = records
            if self.mode == "native-count-mismatch" and self.perf_count == 2:
                native_records -= 1
            actual_records = records
            if self.mode == "offset-mismatch" and self.perf_count == 1:
                actual_records += 1
            self.offset += actual_records
            self.all_messages.extend(str(index) for index in range(records))
            rates = {1: 99999.0, 2: 100.0, 3: 200.0, 4: 300.0}
            return {
                "returncode": 0,
                "stdout": self.perf_summary(
                    native_records, rates[self.perf_count]
                ),
            }
        raise AssertionError("unexpected command: " + repr(argv))

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
            **self.response(argv, input_text),
        }
        return {
            "command": [support.redact(item, redactions) for item in argv],
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "duration_ms": float(10 + len(self.calls)),
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
            code = benchmark.main(arguments or self.arguments())
        result = None
        result_path = self.output / "result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return code, result, stdout.getvalue(), stderr.getvalue()

    def perf_calls(self):
        return [
            call
            for call in self.calls
            if "kafka-producer-perf-test.sh" in call[1]
        ]

    def test_java_home_overrides_older_path_java(self):
        java_home = self.root / "selected-jdk"
        executable = java_home / "bin/java"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        response = self.response

        def conflicting_path(argv, input_text):
            if argv[:2] == ["java", "-version"]:
                return {"returncode": 0, "stderr": 'version "11.0.31"\n'}
            return response(argv, input_text)

        self.response = conflicting_path
        with patch.dict(os.environ, {"JAVA_HOME": str(java_home)}):
            code, result, _, _ = self.invoke()
        self.assertEqual(code, 0)
        java_probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "java_runtime"
        )
        self.assertEqual(java_probe["evidence"]["major"], 17)
        self.assertEqual(java_probe["evidence"]["selected_from"], "JAVA_HOME")
        self.assertEqual(
            java_probe["evidence"]["command"]["command"][0], str(executable)
        )

    def test_success_preserves_rounds_metrics_and_exact_cleanup(self):
        code, result, stdout, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["workload_status"], "passed")
        self.assertEqual(result["cleanup"]["status"], "passed")
        self.assertEqual(json.loads(stdout), result)
        self.assertTrue(result["correctness"]["exact_message_round_trip"])
        self.assertEqual(len(result["rounds"]), 3)
        self.assertEqual(len(self.perf_calls()), 4)
        self.assertEqual(
            result["warmup"]["native"]["records_per_second"], 99999
        )
        self.assertEqual(
            result["aggregates"]["records_per_second_median"], 200.0
        )
        self.assertEqual(result["aggregates"]["timed_records_sent"], 300)
        self.assertTrue(result["final_offset_validation"]["exact"])
        self.assertEqual(self.offset, 313)
        topic = result["ownership"]["topic"]
        group = result["ownership"]["consumer_group"]
        groups = result["ownership"]["consumer_groups_started"]
        self.assertEqual(self.created_topics, [topic])
        self.assertEqual(self.deleted_topics, [topic])
        self.assertEqual(self.deleted_groups, groups)
        self.assertIn(group, groups)
        self.assertEqual(len(groups), 5)
        self.assertEqual(len(set(groups)), len(groups))
        self.assertTrue(topic.startswith("kpbot-bench-"))
        self.assertNotEqual(topic, group)
        for item in result["rounds"]:
            self.assertEqual(item["exit_code"], 0)
            self.assertTrue(item["exact_counts"])
            self.assertTrue(item["identity_validation"]["exact"])
        for call in self.perf_calls():
            self.assertIn("--payload-monotonic", call)
            self.assertNotIn("--record-size", call)
        self.assertTrue((self.output / "warmup-1.stdout.log").is_file())
        self.assertTrue((self.output / "round-3.stdout.log").is_file())

    def test_existing_topic_is_never_created_or_deleted(self):
        code, result, _, _ = self.invoke(self.arguments(create=False))
        self.assertEqual(code, 0)
        self.assertEqual(self.created_topics, [])
        self.assertEqual(self.deleted_topics, [])
        self.assertEqual(result["cleanup"]["topic"], "not_applicable")
        self.assertEqual(
            self.deleted_groups,
            result["ownership"]["consumer_groups_started"],
        )

    def test_nonempty_existing_topic_fails_closed_before_writes(self):
        self.offset = 5
        code, result, _, _ = self.invoke(self.arguments(create=False))
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.perf_calls(), [])
        self.assertEqual(self.created_topics, [])
        self.assertEqual(self.deleted_topics, [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "existing_topic_empty"
        )
        self.assertEqual(probe["category"], "topic_not_empty")

    def test_unsupported_kafka_version_blocks_before_create(self):
        self.mode = "old-kafka"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.created_topics, [])
        self.assertEqual(self.perf_calls(), [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "cli_version"
        )
        self.assertEqual(probe["category"], "wrong_version")

    def test_unsupported_java_version_blocks_before_create(self):
        self.mode = "old-java"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.created_topics, [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "java_runtime"
        )
        self.assertEqual(probe["category"], "wrong_version")

    def test_missing_tool_blocks_before_workload(self):
        (self.kafka / "bin/kafka-get-offsets.sh").unlink()
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.created_topics, [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "official_cli_tools"
        )
        self.assertEqual(probe["category"], "absent")

    def test_authentication_failure_blocks_before_workload(self):
        self.mode = "auth"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.created_topics, [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "authenticated_metadata"
        )
        self.assertEqual(probe["category"], "auth")

    def test_preflight_timeout_is_blocked(self):
        self.mode = "preflight-timeout"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(self.created_topics, [])
        probe = next(
            item
            for item in result["preflight"]
            if item["name"] == "authenticated_metadata"
        )
        self.assertEqual(probe["category"], "timeout")

    def test_round_timeout_keeps_failure_logs_and_stops(self):
        self.timeout_perf_at = 2
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["category"], "timeout")
        self.assertEqual(len(result["rounds"]), 1)
        self.assertEqual(result["rounds"][0]["status"], "failed")
        self.assertTrue(result["rounds"][0]["timed_out"])
        self.assertEqual(len(self.perf_calls()), 2)
        self.assertTrue((self.output / "round-1.stderr.log").is_file())
        self.assertEqual(self.deleted_topics, [result["ownership"]["topic"]])

    def test_round_runtime_failure_records_nonzero_and_stops(self):
        self.fail_perf_at = 3
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["rounds"][-1]["status"], "failed")
        self.assertEqual(result["rounds"][-1]["exit_code"], 42)
        self.assertEqual(len(self.perf_calls()), 3)
        self.assertTrue((self.output / "round-2.stderr.log").is_file())
        self.assertIsNone(result["aggregates"])

    def test_warmup_failure_never_starts_timed_round(self):
        self.fail_perf_at = 1
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["warmup"]["exit_code"], 42)
        self.assertEqual(result["rounds"], [])
        self.assertEqual(len(self.perf_calls()), 1)

    def test_exact_message_mismatch_fails_before_warmup(self):
        self.mode = "bad-messages"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["failure"]["category"], "correctness")
        self.assertFalse(result["correctness"]["exact_message_round_trip"])
        self.assertEqual(self.perf_calls(), [])
        self.assertEqual(
            self.deleted_groups,
            result["ownership"]["consumer_groups_started"],
        )

    def test_native_count_mismatch_fails_despite_zero_exit(self):
        self.mode = "native-count-mismatch"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["failure"]["category"], "correctness")
        self.assertEqual(len(result["rounds"]), 1)
        self.assertFalse(result["rounds"][0]["exact_counts"])

    def test_identity_mismatch_fails_with_matching_counts_and_offsets(self):
        self.mode = "identity-mismatch"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["failure"]["category"], "correctness")
        self.assertEqual(result["workload_status"], "failed")
        self.assertEqual(result["cleanup"]["status"], "passed")
        self.assertEqual(len(result["rounds"]), 1)
        phase = result["rounds"][0]
        self.assertTrue(phase["exact_counts"])
        self.assertFalse(phase["identity_validation"]["exact"])
        self.assertEqual(phase["identity_validation"]["missing_records"], 1)
        self.assertEqual(phase["identity_validation"]["unexpected_records"], 1)

    def test_offset_mismatch_fails_despite_native_count(self):
        self.mode = "offset-mismatch"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["failure"]["category"], "correctness")
        self.assertFalse(result["warmup"]["exact_counts"])
        self.assertEqual(result["rounds"], [])

    def test_ambiguous_create_is_recorded_and_never_deleted(self):
        self.mode = "create-timeout"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(
            result["ownership"]["topic_create_state"], "ambiguous"
        )
        self.assertTrue(result["ownership"]["ambiguous_topic_present"])
        self.assertIn("ambiguous", result["cleanup"]["topic"])
        self.assertEqual(self.deleted_topics, [])
        self.assertEqual(self.perf_calls(), [])
        describe_log = (
            self.output / "topic-create-ambiguous-describe.stdout.log"
        )
        self.assertTrue(describe_log.is_file())

    def test_cleanup_failure_turns_success_into_failure(self):
        self.mode = "cleanup-failure"
        code, result, _, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["workload_status"], "passed")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertIsNotNone(result["aggregates"])
        self.assertTrue(result["cleanup"]["issues"])

    def test_output_collision_preserves_existing_file(self):
        self.output.mkdir()
        sentinel = self.output / "result.json"
        sentinel.write_text("not-json-do-not-overwrite", encoding="utf-8")
        code, result, _, stderr = self.invoke()
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertIn("output_collision", stderr)
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"),
            "not-json-do-not-overwrite",
        )

    def test_secure_config_flags_and_logs_never_expose_secret(self):
        config = self.root / "secure.properties"
        config.write_text(
            "security.protocol=SASL_SSL\n"
            "sasl.jaas.config=org.example.Login required "
            f'password="{self.secret}";\n',
            encoding="utf-8",
        )
        config.chmod(0o600)
        code, result, stdout, _ = self.invoke(
            self.arguments() + ["--client-config", str(config)]
        )
        self.assertEqual(code, 0)
        evidence = json.dumps(result) + stdout
        for path in self.output.iterdir():
            if path.is_file():
                evidence += path.read_text(encoding="utf-8")
        self.assertNotIn(self.secret, evidence)
        joined = [item for call in self.calls for item in call]
        self.assertIn("--command-config", joined)
        self.assertIn("--producer.config", joined)
        self.assertIn("--consumer.config", joined)
        self.assertNotIn(self.secret, joined)

    def test_invalid_business_group_config_is_blocked(self):
        config = self.root / "business.properties"
        config.write_text("group.id=business\n", encoding="utf-8")
        config.chmod(0o600)
        code, result, _, _ = self.invoke(
            self.arguments() + ["--client-config", str(config)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(result["preflight"][0]["category"], "invalid_config")

    def test_unbounded_or_nonfinite_arguments_are_rejected(self):
        invalid = (
            ("--timeout", "nan"),
            ("--timeout", "0"),
            ("--rounds", "2"),
            ("--records", "0"),
            ("--throughput", "-1"),
            ("--records", "1000001"),
        )
        for flag, value in invalid:
            with self.subTest(flag=flag, value=value):
                arguments = self.arguments()
                if flag in arguments:
                    arguments[arguments.index(flag) + 1] = value
                else:
                    arguments.extend((flag, value))
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    benchmark.build_parser().parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_parser_uses_final_native_summary_and_explicit_units(self):
        text = (
            "5 records sent, 1.0 records/sec (0.01 MB/sec), "
            "1.0 ms avg latency, 2.0 ms max latency.\n"
            + self.perf_summary(100, 200.0)
        )
        metrics = benchmark.parse_perf_summary(text)
        self.assertEqual(metrics["records_sent"], 100)
        self.assertEqual(metrics["p95_latency_ms"], 8)
        self.assertIn("MiB/s", metrics["throughput_unit_note"])

    def test_timeout_kills_the_bounded_process_group(self):
        process = Mock()
        process.pid = 12345
        process.communicate.side_effect = (
            subprocess.TimeoutExpired("kafka", 1),
            ("", ""),
        )
        process.returncode = -9
        killpg = Mock()
        with (
            patch.object(
                support.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(support.os, "killpg", killpg),
        ):
            result = support.run_process(["bash", "tool"], 1)
        self.assertTrue(result["timed_out"])
        killpg.assert_called_once_with(12345, signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(popen.call_args.kwargs["shell"])

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
                str(target / "scripts/run_benchmark.py"),
                "--help",
            ],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("bounded synthetic producer benchmark", completed.stdout)
        self.assertTrue((target / "references/probes.md").is_file())
        self.assertTrue((target / "references/metrics.md").is_file())
        inspected = (target / "SKILL.md").read_text(encoding="utf-8")
        inspected += (target / "scripts/run_benchmark.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("kptools-kafka-environment-check", inspected)
        self.assertNotIn(".input", inspected)

    def test_keyboard_interrupt_kills_group_bounded_drains_and_reraises(self):
        process = Mock()
        process.pid = 12346
        process.communicate.side_effect = (
            KeyboardInterrupt(),
            ("partial-output", "partial-error"),
        )
        process.returncode = -9
        killpg = Mock()
        with (
            patch.object(support.subprocess, "Popen", return_value=process),
            patch.object(support.os, "killpg", killpg),
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            support.run_process(["bash", "tool"], 1)
        killpg.assert_called_once_with(12346, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(
            process.communicate.call_args_list[1].kwargs["timeout"],
            support.DRAIN_TIMEOUT_SECONDS,
        )
        interrupted = raised.exception.process_result
        self.assertEqual(interrupted["error_category"], "interrupted")
        self.assertFalse(interrupted["drain_timed_out"])
        self.assertEqual(interrupted["stdout"], "partial-output")

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
        BenchmarkContractTests
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
