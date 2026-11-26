"""Offline tests of the native Kafka produce/consume validation flow."""

import base64
import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_service.py"
GUIDE = Path(__file__).resolve().parents[2] / "references/guide.md"
spec = importlib.util.spec_from_file_location("kafka_check", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

JAVA = None
if os.environ.get("JAVA_HOME"):
    candidate = Path(os.environ["JAVA_HOME"]) / "bin" / "java"
    if candidate.is_file():
        JAVA = str(candidate)
if JAVA is None:
    JAVA = shutil.which("java")
BASH = shutil.which("bash")

JDK_PROPERTIES_SOURCE = """import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.List;
import java.util.Properties;

public class JdkProperties {
    public static void main(String[] args) throws Exception {
        Properties properties = new Properties();
        try (Reader reader = new InputStreamReader(
                new FileInputStream(args[0]), StandardCharsets.UTF_8)) {
            properties.load(reader);
        }
        List<String> keys = new ArrayList<>(properties.stringPropertyNames());
        Collections.sort(keys);
        Base64.Encoder encoder = Base64.getEncoder();
        for (String key : keys) {
            String value = properties.getProperty(key);
            System.out.println(
                encoder.encodeToString(key.getBytes(StandardCharsets.UTF_8))
                + "\t"
                + encoder.encodeToString(
                    value.getBytes(StandardCharsets.UTF_8))
            );
        }
    }
}
"""

JAVA_PROPERTIES_FIXTURES = {
    "comment-hash-backslash": "# comment\\\ngroup.id=business\n",
    "comment-bang-backslash": "! comment\\\ngroup.id=business\n",
    "comment-formfeed": "# comment\fgroup.id=business\n",
    "leading-formfeed": "\fgroup.id=business\n",
    "continued-key": "group\\\n.id=business\n",
    "escaped-key": "groups\\.id=business\n",
    "unicode-key": "\\u0067roup.id=business\n",
    "continued-value": "group.id=busi\\\n    ness\n",
    "crlf": (
        "group.id=business\r\nmy.group.id=other\rsecurity.protocol=PLAINTEXT\n"
    ),
    "continued-comment-marker": "key=value\\\n#not-a-comment\n",
    "double-comment-backslash": "# first\\\n! second\\\ngroup.id=business\n",
    "trailing-backslash": "key=value\\",
}


def jdk_properties(path):
    source = Path(path).with_name("JdkProperties.java")
    source.write_text(JDK_PROPERTIES_SOURCE, encoding="utf-8")
    completed = subprocess.run(
        [JAVA, str(source), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    properties = {}
    for line in completed.stdout.splitlines():
        key, value = line.split("\t", 1)
        properties[base64.b64decode(key).decode("utf-8")] = base64.b64decode(
            value
        ).decode("utf-8")
    return properties


class KafkaCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "results"
        self.args = module.parser().parse_args(
            ["--kafka-dir", "kafka", "--output", str(self.output)]
        )
        self.calls = []
        self.budgets = []
        self.messages = ""
        self.bad_messages = False
        self.fail_at = None
        self.timeout_at = None

    def config(self, name, content):
        path = Path(self.temp.name) / name
        path.write_text(content, encoding="utf-8")
        self.args.client_config = path
        return path

    def process(self, argv, **kwargs):
        test = self
        index = len(self.calls)
        self.calls.append(argv)

        class Process:
            pid = 12345
            returncode = 1 if index == test.fail_at else 0
            attempts = 0

            def communicate(self, input=None, timeout=None):
                test.budgets.append(timeout)
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
        result = self.run_check()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            result["cleanup"],
            "topic creation result unknown; verify topic before cleanup",
        )
        create_topic = self.calls[0][self.calls[0].index("--topic") + 1]
        self.assertEqual(result["topic"], create_topic)

    def test_create_timeout_persists_unknown_status_before_request(self):
        self.timeout_at = 0
        snapshots = []

        def process(argv, **kwargs):
            snapshots.append(
                json.loads((self.output / "result.json").read_text())
            )
            return self.process(argv, **kwargs)

        with (
            patch.object(module.subprocess, "Popen", side_effect=process),
            patch.object(module.os, "killpg", create=True),
        ):
            result = module.check(self.args)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(snapshots), 1)
        pending = snapshots[0]
        self.assertEqual(pending["topic"], result["topic"])
        self.assertEqual(
            pending["cleanup"],
            "topic creation result unknown; verify topic before cleanup",
        )
        self.assertEqual(result["cleanup"], pending["cleanup"])
        self.assertNotEqual(result["cleanup"], "no topic created")
        self.assertEqual(
            result, json.loads((self.output / "result.json").read_text())
        )
        self.assertTrue((self.output / "00.stderr").exists())

    def test_produce_failure_never_reports_delivery_success(self):
        self.fail_at = 2
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertEqual(len(self.calls), 3)

    def test_wrong_messages_fail_despite_zero_exit(self):
        self.bad_messages = True
        self.assertEqual(self.run_check()["status"], "failed")

    def test_cli_deprecation_banners_do_not_corrupt_comparison(self):
        banners = (
            "Option --consumer-property is deprecated and will be removed "
            "in a future version. Use --command-property instead.\n"
            "Option --consumer.config is deprecated and will be removed in "
            "a future version. Use --command-config instead.\n"
        )
        original = self.process

        def with_banners(argv, **kwargs):
            process = original(argv, **kwargs)
            communicate = process.communicate

            def wrapped(input=None, timeout=None):
                stdout, stderr = communicate(input=input, timeout=timeout)
                if "kafka-console-consumer.sh" in argv[1]:
                    stdout = banners + stdout
                return stdout, stderr

            process.communicate = wrapped
            return process

        with (
            patch.object(module.subprocess, "Popen", side_effect=with_banners),
            patch.object(module.os, "killpg", create=True),
        ):
            result = module.check(self.args)
        self.assertEqual(result["status"], "passed")
        # A wrong payload still fails even with banners present.
        self.bad_messages = True
        self.args.output = Path(self.temp.name) / "banner-wrong"
        with (
            patch.object(module.subprocess, "Popen", side_effect=with_banners),
            patch.object(module.os, "killpg", create=True),
        ):
            self.assertEqual(module.check(self.args)["status"], "failed")

    def test_timeout_stops_and_retains_logs(self):
        self.timeout_at = 0
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertTrue((self.output / "00.stderr").exists())
        self.assertEqual(len(self.calls), 1)

    def test_client_config_routed_to_each_native_tool(self):
        self.config(
            "auth.properties",
            "security.protocol=SASL_PLAINTEXT\nsasl.mechanism=PLAIN\n",
        )
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

    def test_timeout_propagates_to_consumer_and_processes(self):
        for seconds in (5, 60, 0.05, 0.0001):
            with self.subTest(seconds=seconds):
                self.args.timeout = seconds
                self.args.output = self.output / str(seconds)
                self.calls.clear()
                self.budgets.clear()
                self.assertEqual(self.run_check()["status"], "passed")
                argv = self.calls[-1]
                self.assertEqual(
                    argv[argv.index("--timeout-ms") + 1],
                    str(max(1, int(seconds * 1000))),
                )
                self.assertEqual(self.budgets, [seconds] * 4)

    def test_consumer_timeout_reports_failure_and_retains_logs(self):
        self.timeout_at = 3
        self.assertEqual(self.run_check()["status"], "failed")
        self.assertTrue((self.output / "03.stderr").exists())

    def test_posix_timeout_kills_the_process_group(self):
        self.timeout_at = 3
        killpg = Mock()
        with (
            patch.object(module, "signal", SimpleNamespace(SIGKILL=9)),
            patch.object(
                module, "os", SimpleNamespace(name="posix", killpg=killpg)
            ),
            patch.object(
                module.subprocess, "Popen", side_effect=self.process
            ) as popen,
        ):
            self.assertEqual(module.check(self.args)["status"], "failed")
        killpg.assert_called_once_with(12345, 9)
        self.assertTrue(
            all(
                call.kwargs["start_new_session"]
                for call in popen.call_args_list
            )
        )

    def test_consumer_is_bounded(self):
        self.run_check()
        consumer = self.calls[-1]
        self.assertIn("--max-messages", consumer)
        self.assertIn("--timeout-ms", consumer)
        group = consumer[consumer.index("--group") + 1]
        self.assertTrue(group.startswith("kpbot-smoke-"))
        topic = self.calls[0][self.calls[0].index("--topic") + 1]
        self.assertNotEqual(group, topic)

    def test_default_topic_replication_inherits_broker_default(self):
        self.assertIsNone(self.args.replication_factor)
        self.run_check()
        self.assertNotIn("--replication-factor", self.calls[0])

    def test_explicit_single_node_replication_factor(self):
        self.args.replication_factor = 1
        self.run_check()
        create = self.calls[0]
        self.assertEqual(create[create.index("--replication-factor") + 1], "1")

    def test_replication_factor_must_be_positive(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(value=value):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    module.parser().parse_args(
                        [
                            "--kafka-dir",
                            "kafka",
                            "--output",
                            "out",
                            "--replication-factor",
                            value,
                        ]
                    )

    def test_configured_group_rejected_before_topic_creation(self):
        self.config(
            "business.properties",
            "bootstrap.servers=localhost:9092\ngroup.id=payments\n",
        )
        result = self.run_check()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("group.id", result["error"])
        self.assertEqual(self.calls, [])
        self.assertEqual(result["cleanup"], "no topic created")
        self.assertTrue((self.output / "result.json").exists())

    def test_group_id_detected_with_java_escapes_and_continuations(self):
        dangerous = (
            "group\\.id=business\n",
            "group\\\n.id=business\n",
            "\\u0067roup.id=business\n",
            "group.id\\\n = business\n",
            "  group.id : business\n",
            "group.id\tbusiness\n",
            "# comment\\\ngroup.id=business\n",
            "! comment\\\ngroup.id=business\n",
        )
        for index, content in enumerate(dangerous):
            with self.subTest(content=content):
                self.config(f"dangerous-{index}.properties", content)
                self.output = Path(self.temp.name) / f"dangerous-{index}"
                self.args.output = self.output
                self.calls.clear()
                result = self.run_check()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["exit_code"], 2)
                self.assertEqual(self.calls, [])

    def test_unrelated_or_commented_group_id_is_not_rejected(self):
        self.config(
            "safe.properties",
            "# group.id=old-business\n"
            "! group.id=another\n"
            "# comment\fgroup.id=swallowed-by-comment\n"
            "my.group.id=prefixed\n"
            "group.id.extra=other\n"
            "security.protocol=SSL\n",
        )
        result = self.run_check()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.calls), 4)

    def test_missing_client_config_is_rejected_before_topic_creation(self):
        self.args.client_config = Path(self.temp.name) / "missing.properties"
        result = self.run_check()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("cannot read --client-config", result["error"])
        self.assertEqual(self.calls, [])

    def test_java_properties_parser_matches_java_rules(self):
        properties = module.java_properties(
            "# group.id=ignored\n"
            "group\\\n"
            ".id = first\n"
            "groups\\.id=business\n"
            "\\u0067roup.id=business\n"
            "group.id=busi\\\n"
            "    ness\n"
            "my.group.id.extra=other\n"
        )
        self.assertEqual(properties.get("group.id"), "business")
        self.assertEqual(properties.get("groups.id"), "business")
        self.assertEqual(properties.get("my.group.id.extra"), "other")
        self.assertNotIn("group.id", module.java_properties("# group.id=x\n"))

    @unittest.skipUnless(JAVA, "java executable not available")
    def test_parser_matches_jdk_properties_load(self):
        for name, content in JAVA_PROPERTIES_FIXTURES.items():
            with self.subTest(fixture=name):
                path = Path(self.temp.name) / f"{name}.properties"
                path.write_text(content, encoding="utf-8")
                self.assertEqual(
                    module.java_properties(content), jdk_properties(path)
                )

    def test_configured_group_value_is_not_used_as_run_group(self):
        self.config("safe.properties", "security.protocol=SSL\n")
        result = self.run_check()
        consumer = self.calls[-1]
        self.assertEqual(
            consumer[consumer.index("--group") + 1],
            result["consumer_group"],
        )


@unittest.skipUnless(BASH, "bash executable not available")
class KafkaBenchmarkGuideTests(unittest.TestCase):
    def run_example(self, fail_at, block_exit_log=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "count").write_text("0\n", encoding="utf-8")
            tool = root / "bin/kafka-producer-perf-test.sh"
            tool.write_text(
                """#!/usr/bin/env bash
count=$(cat "$ROOT/count")
count=$((count + 1))
printf '%s\\n' "$count" > "$ROOT/count"
printf 'invocation %s\\n' "$count"
if [ "$count" -eq "$FAIL_AT" ]; then
    exit 42
fi
""",
                encoding="utf-8",
            )
            tool.chmod(0o755)
            guide = GUIDE.read_text(encoding="utf-8")
            start = guide.index("mkdir -p results\nEXP=results/bench-001")
            end = guide.index("\n```", start)
            script = guide[start:end]
            if block_exit_log:
                script = script.replace(
                    "# 预热：少量消息，单独文件",
                    'mkdir "$EXP/exit-codes.log"\n\n'
                    "# 预热：少量消息，单独文件",
                )
            completed = subprocess.run(
                [BASH, "-e", "-s"],
                input=script,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                env=dict(os.environ, ROOT=str(root), FAIL_AT=str(fail_at)),
            )
            results = root / "results/bench-001"
            logs = {
                path.name: path.read_text(encoding="utf-8")
                for path in results.glob("*.log")
                if path.is_file()
            }
            calls = int((root / "count").read_text(encoding="utf-8"))
            return completed, logs, calls

    def test_exit_code_log_write_failure_stops_after_warmup(self):
        completed, logs, calls = self.run_example(0, block_exit_log=True)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("exit-codes.log", completed.stderr)
        self.assertEqual(calls, 1)
        self.assertEqual(logs, {"warmup-1.log": "invocation 1\n"})

    def test_example_stops_on_failure_and_retains_each_exit_code(self):
        rounds = ("warmup-1", "round-1", "round-2", "round-3")
        for fail_at in range(len(rounds) + 1):
            with self.subTest(fail_at=fail_at):
                completed, logs, calls = self.run_example(fail_at)
                attempted = fail_at if fail_at else len(rounds)
                expected_code = 42 if fail_at else 0
                self.assertEqual(
                    completed.returncode, expected_code, completed.stderr
                )
                self.assertEqual(calls, attempted)
                expected_exit_codes = "".join(
                    f"{name} exit_code={42 if index + 1 == fail_at else 0}\n"
                    for index, name in enumerate(rounds[:attempted])
                )
                self.assertEqual(logs["exit-codes.log"], expected_exit_codes)
                self.assertEqual(
                    set(logs),
                    {"exit-codes.log"}
                    | {f"{name}.log" for name in rounds[:attempted]},
                )
                for index, name in enumerate(rounds[:attempted], start=1):
                    self.assertEqual(
                        logs[f"{name}.log"], f"invocation {index}\n"
                    )


class KafkaScriptEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kafka = self.root / "kafka"
        (self.kafka / "bin").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.tool(
            "kafka-topics.sh",
            """
topic=""
action=""
while [ $# -gt 0 ]; do
    case "$1" in
        --topic) topic="$2"; shift 2 ;;
        --create) action=create; shift ;;
        --describe) action=describe; shift ;;
        --replication-factor) echo "rf=$2" >> "$STATE/args"; shift 2 ;;
        *) shift ;;
    esac
done
if [ "$action" = "create" ]; then
    printf '%s\\n' "$topic" >> "$STATE/created"
    echo "Created topic $topic."
elif [ "$action" = "describe" ]; then
    echo "Topic: $topic PartitionCount: 1 ReplicationFactor: 1"
fi
""",
        )
        self.tool(
            "kafka-console-producer.sh",
            """
cat > "$STATE/messages"
""",
        )
        self.tool(
            "kafka-console-consumer.sh",
            """
while [ $# -gt 0 ]; do
    case "$1" in
        --group) echo "$2" > "$STATE/group"; shift 2 ;;
        *) shift ;;
    esac
done
cat "$STATE/messages"
""",
        )

    def tool(self, name, body):
        (self.kafka / "bin" / name).write_text(body, encoding="utf-8")

    def run_script(self, *arguments, config=None):
        output = self.root / "results"
        command = [
            sys.executable,
            str(SCRIPT),
            "--kafka-dir",
            str(self.kafka),
            "--bootstrap",
            "localhost:9092",
            "--output",
            str(output),
            *arguments,
        ]
        if config is not None:
            command += ["--client-config", str(config)]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=dict(os.environ, STATE=str(self.state)),
        )
        result = json.loads((output / "result.json").read_text())
        return completed, result

    def test_script_omits_replication_factor_and_uses_run_group(self):
        completed, result = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["status"], "passed")
        args_file = self.state / "args"
        if args_file.exists():
            self.assertNotIn("rf=", args_file.read_text())
        topic = self.state.joinpath("created").read_text().splitlines()[0]
        self.assertEqual(
            self.state.joinpath("messages").read_text().splitlines(),
            [f"{topic}:{index}" for index in range(3)],
        )
        self.assertEqual(
            self.state.joinpath("group").read_text().strip(),
            result["consumer_group"],
        )

    def test_script_passes_explicit_single_node_replication_factor(self):
        completed, result = self.run_script("--replication-factor", "1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["replication_factor"], 1)
        self.assertEqual(
            self.state.joinpath("args").read_text().splitlines(), ["rf=1"]
        )

    def test_script_rejects_group_id_without_touching_kafka(self):
        config = self.root / "business.properties"
        config.write_text(
            "security.protocol=PLAINTEXT\ngroup\\\n.id=preexisting\n",
            encoding="utf-8",
        )
        completed, result = self.run_script(config=config)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["cleanup"], "no topic created")
        self.assertEqual(list(self.state.iterdir()), [])


@unittest.skipUnless(
    BASH and shutil.which("sha512sum"), "Bash and sha512sum required"
)
class InstallChecksumGuideTests(unittest.TestCase):
    def test_documented_gate_accepts_valid_and_rejects_bad_checksums(self):
        example = (
            GUIDE.read_text(encoding="utf-8")
            .split("```bash\n", 1)[1]
            .split("```", 1)[0]
        )
        self.assertLess(
            example.index("actual_sha512="), example.index("tar -tzf")
        )
        gate = example[
            example.index("expected_sha512=") : example.index("tar -tzf")
        ]
        data = b"deterministic-release-fixture\n"
        digest = hashlib.sha512(data).hexdigest().upper()
        for mode in ("valid", "mismatch", "malformed"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "kafka.tgz").write_bytes(data)
                selected = digest
                if mode == "mismatch":
                    selected = ("0" if digest[0] != "0" else "1") + digest[1:]
                groups = [
                    selected[index : index + 8] for index in range(0, 128, 8)
                ]
                sidecar = (
                    "kafka_2.13-4.4.0.tgz: "
                    + " \n  ".join(
                        " ".join(groups[index : index + 4])
                        for index in range(0, 16, 4)
                    )
                    + "\n"
                )
                if mode == "malformed":
                    sidecar = "not-a-checksum\n"
                (root / "kafka.tgz.sha512").write_text(
                    sidecar, encoding="utf-8"
                )
                completed = subprocess.run(
                    [BASH, "-c", gate],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode == 0,
                    mode == "valid",
                    completed.stderr,
                )


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
