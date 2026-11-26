#!/usr/bin/env python3
"""Offline contract regressions for the ClickHouse environment probes."""

from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_environment.py"
)
SPEC = importlib.util.spec_from_file_location("clickhouse_environment", SCRIPT)
ENVIRONMENT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENVIRONMENT_MODULE)
FORKED_DESCENDANT = r"""
import os
import signal
import time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(os.getpid(), flush=True)
    while True:
        time.sleep(60)
os.waitpid(pid, 0)
"""
FAKE_CLIENT = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parent
config_path = root / "fake-config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if "--version" in args:
    if "benchmark" in Path(__file__).name or args[:1] == ["benchmark"]:
        print("UNRECOGNIZED_ARGUMENTS: benchmark --version", file=sys.stderr)
        raise SystemExit(40)
    print("ClickHouse client version " + config["tool_version"])
    raise SystemExit(0)
if "--help" in args:
    print(" ".join(config["help_flags"]))
    raise SystemExit(0)
query = args[args.index("--query") + 1] if "--query" in args else ""
if config.get("sleep_query") and config["sleep_query"] in query:
    time.sleep(10)
if config.get("auth_error") and query == "SELECT version()":
    print(
        "Code: 516. AUTHENTICATION_FAILED password="
        + os.environ.get("CLICKHOUSE_PASSWORD", ""),
        file=sys.stderr,
    )
    raise SystemExit(1)
if query == "SELECT version()":
    print(config["server_version"])
elif query == "SELECT sum(number) FROM numbers(1000)":
    print(config.get("aggregate", "499500"))
else:
    print("unexpected query", file=sys.stderr)
    raise SystemExit(3)
"""
DEFAULT_CONFIG = {
    "tool_version": "26.8.42.13",
    "server_version": "26.8.42.13",
    "help_flags": [
        "--query",
        "--iterations",
        "--concurrency",
        "--delay",
        "--client-side-time",
        "--config",
    ],
}


class EnvironmentContractTests(unittest.TestCase):
    """Exercise real script processes against a bounded fake official CLI."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "fake-clickhouse"
        self.benchmark = self.root / "fake-clickhouse-benchmark"
        self.secret = "contract-only-secret"
        self.env = dict(os.environ, CLICKHOUSE_PASSWORD=self.secret)

    def fake_client(self, **changes):
        config = dict(DEFAULT_CONFIG)
        config.update(changes)
        (self.root / "fake-config.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        self.client.write_text(FAKE_CLIENT, encoding="utf-8")
        self.client.chmod(0o700)
        self.benchmark.write_text(FAKE_CLIENT, encoding="utf-8")
        self.benchmark.chmod(0o700)
        return self.client

    def invoke(self, output, benchmark=None, extra=()):
        command = [
            sys.executable,
            str(SCRIPT),
            "--client",
            str(self.client),
            "--benchmark",
            str(benchmark or self.benchmark),
            "--timeout",
            "0.2",
            "--output",
            str(output),
            *map(str, extra),
        ]
        return subprocess.run(
            command,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )

    def result(self, output):
        return json.loads((output / "result.json").read_text(encoding="utf-8"))

    def calls(self):
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_help_lists_series_exit_codes(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for fragment in (
            "0=ready",
            "1=blocked",
            "2=argument/output error or collision",
            "3=unknown",
        ):
            self.assertIn(fragment, completed.stdout)

    def test_ready_checks_live_interface_and_exact_aggregate(self):
        self.fake_client()
        output = self.root / "new" / "ready"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["ready"])
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(checks["server_interface"]["status"], "ready")
        self.assertEqual(checks["exact_query"]["evidence"]["actual"], "499500")
        self.assertTrue(checks["exact_query"]["mandatory"])
        self.assertFalse(checks["client_server_series"]["mandatory"])
        self.assertIn(
            "--client-side-time",
            checks["benchmark_interface"]["evidence"]["required_flags"],
        )
        self.assertIn("not a production-health", result["limitations"][1])

    def test_unsupported_server_version_is_blocked(self):
        self.fake_client(server_version="22.7.9.1")
        output = self.root / "old"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        check = next(
            item
            for item in result["checks"]
            if item["name"] == "server_interface"
        )
        self.assertEqual(check["reason"], "wrong_version")
        self.assertEqual(check["evidence"]["minimum_version"], "22.8.0")
        self.assertEqual(result["status"], "blocked")

    def test_missing_benchmark_tool_is_not_ready(self):
        self.fake_client()
        output = self.root / "missing"
        completed = self.invoke(output, benchmark=self.root / "not-installed")
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        check = next(
            item
            for item in result["checks"]
            if item["name"] == "benchmark_interface"
        )
        self.assertEqual(check["reason"], "missing_tool")
        self.assertEqual(result["status"], "blocked")

    def test_authentication_failure_is_classified_and_redacted(self):
        self.secret = "AUTHENTICATION_FAILED"
        self.env["CLICKHOUSE_PASSWORD"] = self.secret
        self.fake_client(auth_error=True)
        output = self.root / "auth"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        server = next(
            item
            for item in result["checks"]
            if item["name"] == "server_interface"
        )
        exact = next(
            item for item in result["checks"] if item["name"] == "exact_query"
        )
        self.assertEqual(server["reason"], "auth")
        self.assertEqual(exact["reason"], "dependency_failed")
        log_text = (output / "server-interface.stderr.log").read_text()
        evidence = completed.stdout + log_text
        self.assertNotIn(self.secret, evidence)
        self.assertIn("[REDACTED]", evidence)

    def test_timeout_preserves_failure_evidence(self):
        self.fake_client(sleep_query="SELECT version()")
        output = self.root / "timeout"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        server = next(
            item
            for item in result["checks"]
            if item["name"] == "server_interface"
        )
        self.assertEqual(server["reason"], "timeout")
        self.assertTrue((output / "server-interface.stdout.log").exists())
        self.assertIsNotNone(server["evidence"]["duration_ms"])

    def test_timeout_kills_sigterm_ignoring_forked_descendant(self):
        started = time.monotonic()
        outcome = ENVIRONMENT_MODULE.run_process(
            [sys.executable, "-c", FORKED_DESCENDANT], timeout=0.2
        )
        elapsed = time.monotonic() - started
        self.assertTrue(outcome["timed_out"])
        self.assertLess(elapsed, 2.5)
        descendant_pid = int(outcome["stdout"].strip())
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    break
                # A container's non-reaping PID 1 may retain a dead zombie.
                try:
                    state = Path(f"/proc/{descendant_pid}/stat").read_text()
                except FileNotFoundError:
                    break
                if state.rpartition(") ")[2].split()[0] == "Z":
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        finally:
            try:
                os.kill(descendant_pid, 0)
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass

    def test_cancellation_kills_real_child_and_reraises(self):
        real_popen = ENVIRONMENT_MODULE.subprocess.Popen
        created = []

        class InterruptOnce:
            def __init__(self, process):
                self.process = process
                self.first = True

            def communicate(self, timeout=None):
                if self.first:
                    self.first = False
                    raise KeyboardInterrupt
                return self.process.communicate(timeout=timeout)

            def __getattr__(self, name):
                return getattr(self.process, name)

        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            created.append(process)
            return InterruptOnce(process)

        started = time.monotonic()
        with mock.patch.object(
            ENVIRONMENT_MODULE.subprocess, "Popen", side_effect=popen
        ):
            with self.assertRaises(KeyboardInterrupt):
                ENVIRONMENT_MODULE.run_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=30,
                )
        self.assertLess(time.monotonic() - started, 2.5)
        child_pid = created[0].pid
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        finally:
            try:
                os.kill(child_pid, 0)
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass

    def test_wrong_aggregate_fails_closed(self):
        self.fake_client(aggregate="0")
        output = self.root / "wrong-sum"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        exact = next(
            item for item in result["checks"] if item["name"] == "exact_query"
        )
        self.assertEqual(exact["reason"], "correctness")
        self.assertEqual(exact["evidence"]["expected"], "499500")
        self.assertEqual(exact["evidence"]["actual"], "0")

    def test_numeric_password_does_not_change_correctness_semantics(self):
        self.secret = "499500"
        self.env["CLICKHOUSE_PASSWORD"] = self.secret
        self.fake_client()
        output = self.root / "numeric-password"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        exact = next(
            item for item in result["checks"] if item["name"] == "exact_query"
        )
        self.assertEqual(exact["status"], "ready")
        self.assertEqual(exact["evidence"]["actual"], "[REDACTED]")
        self.assertEqual(
            (output / "exact-query.stdout.log").read_text(encoding="utf-8"),
            "[REDACTED]\n",
        )

    def test_unknown_version_has_distinct_exit_and_status(self):
        self.fake_client(server_version="unparseable")
        output = self.root / "unknown"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(self.result(output)["status"], "unknown")

    def test_missing_required_interface_is_wrong_version(self):
        flags = list(DEFAULT_CONFIG["help_flags"])
        flags.remove("--client-side-time")
        self.fake_client(help_flags=flags)
        output = self.root / "interface"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        interface = next(
            item
            for item in result["checks"]
            if item["name"] == "benchmark_interface"
        )
        self.assertEqual(interface["reason"], "wrong_version")
        self.assertEqual(
            interface["evidence"]["missing_flags"], ["--client-side-time"]
        )

    def test_tls_password_example_cleans_up_on_success_and_failure(self):
        skill_text = (SCRIPT.parents[1] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        example = next(
            block.split("```", 1)[0]
            for block in skill_text.split("```bash\n")[1:]
            if "read -rsp" in block
        )
        self.assertIn("trap 'unset CLICKHOUSE_PASSWORD' EXIT", example)
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """import json
import os
from pathlib import Path
import sys

Path(os.environ["CAPTURE"]).write_text(
    json.dumps({
        "password": os.environ.get("CLICKHOUSE_PASSWORD"),
        "argv": sys.argv[1:],
    }),
    encoding="utf-8",
)
raise SystemExit(int(os.environ["EXPECTED_STATUS"]))
""",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        for command_status in (0, 23):
            with self.subTest(command_status=command_status):
                capture = self.root / f"password-example-{command_status}.json"
                wrapper = (
                    "export CLICKHOUSE_PASSWORD=parent-marker\n"
                    + example
                    + "\nexample_rc=$?\n"
                    + '[ "$example_rc" -eq "$EXPECTED_STATUS" ] || exit 90\n'
                    + '[ "$CLICKHOUSE_PASSWORD" = parent-marker ] || exit 91\n'
                )
                env = dict(
                    self.env,
                    CAPTURE=str(capture),
                    EXPECTED_STATUS=str(command_status),
                    PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                )
                completed = subprocess.run(
                    ["bash", "-c", wrapper],
                    cwd=SCRIPT.parents[1],
                    env=env,
                    input=self.secret + "\n",
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                observed = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(observed["password"], self.secret)
                argv_text = "\n".join(observed["argv"])
                self.assertNotIn(self.secret, argv_text)
                self.assertNotIn("--password", argv_text)
                self.assertIn("scripts/check_environment.py", observed["argv"])
                self.assertIn("--secure", observed["argv"])

    def test_omitted_user_respects_supplied_config(self):
        self.fake_client()
        config = self.root / "client.xml"
        config.write_text(
            "<clickhouse><user>config-user</user></clickhouse>",
            encoding="utf-8",
        )
        config.chmod(0o600)
        output = self.root / "config-user"
        completed = self.invoke(output, extra=("--config", config))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertEqual(result["target"]["user"], "config-user")
        self.assertEqual(result["target"]["user_source"], "config")
        query_calls = [call for call in self.calls() if "--query" in call]
        self.assertTrue(query_calls)
        for call in query_calls:
            self.assertEqual(call[call.index("--user") + 1], "config-user")
            self.assertIn(str(config), call)

    def test_invalid_config_fails_before_output_or_probes(self):
        self.fake_client()
        for index, content in enumerate(
            ("<clickhouse><user></clickhouse>", "<clickhouse><user/></clickhouse>")
        ):
            with self.subTest(content=content):
                config = self.root / f"invalid-{index}.xml"
                config.write_text(content, encoding="utf-8")
                config.chmod(0o600)
                output = self.root / f"invalid-config-{index}"
                completed = self.invoke(output, extra=("--config", config))
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(output.exists())
                self.assertEqual(self.calls(), [])

    def test_explicit_user_still_overrides_config(self):
        self.fake_client()
        config = self.root / "client.xml"
        config.write_text(
            "<clickhouse><user>config-user</user></clickhouse>",
            encoding="utf-8",
        )
        config.chmod(0o600)
        output = self.root / "explicit-user"
        completed = self.invoke(
            output,
            extra=("--config", config, "--user", "cli-user"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertEqual(result["target"]["user"], "cli-user")
        self.assertEqual(result["target"]["user_source"], "cli")
        version_call = next(
            call for call in self.calls() if "SELECT version()" in call
        )
        self.assertEqual(
            version_call[version_call.index("--user") + 1], "cli-user"
        )

    def test_omitted_user_without_config_uses_client_default(self):
        self.fake_client()
        output = self.root / "default-user"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertEqual(result["target"]["user"], "default")
        self.assertEqual(result["target"]["user_source"], "client_default")
        query_calls = [call for call in self.calls() if "--query" in call]
        self.assertTrue(query_calls)
        for call in query_calls:
            self.assertEqual(call[call.index("--user") + 1], "default")

    def test_secure_nondefault_target_never_puts_password_in_argv(self):
        self.fake_client()
        config = self.root / "private-client.xml"
        config.write_text("<clickhouse/>", encoding="utf-8")
        config.chmod(0o600)
        output = self.root / "secure"
        completed = self.invoke(
            output,
            extra=(
                "--host",
                "db.example",
                "--port",
                "9440",
                "--user",
                "benchmark-user",
                "--secure",
                "--config",
                config,
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        flattened = (
            "\n".join("\n".join(call) for call in self.calls())
            + completed.stdout
        )
        self.assertNotIn(self.secret, flattened)
        self.assertNotIn("--password", flattened)
        version_call = next(
            call for call in self.calls() if "SELECT version()" in call
        )
        self.assertIn("db.example", version_call)
        self.assertIn("9440", version_call)
        self.assertIn(str(config), version_call)
        self.assertIn("--secure", version_call)

    def test_single_binary_never_invokes_benchmark_version(self):
        self.fake_client()
        output = self.root / "single-binary"
        completed = self.invoke(output, extra=("--single-binary",))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        version_calls = [call for call in self.calls() if "--version" in call]
        self.assertEqual(version_calls, [["--version"]])
        result = self.result(output)
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(
            checks["client_tool"]["evidence"]["version"], "26.8.42.13"
        )
        self.assertEqual(
            checks["benchmark_interface"]["evidence"]["version"],
            "26.8.42.13",
        )

    def test_output_collision_preserves_sentinel_and_skips_probes(self):
        self.fake_client()
        output = self.root / "collision"
        output.mkdir()
        sentinel = output / "result.json"
        sentinel.write_text("keep me", encoding="utf-8")
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.calls(), [])

    def test_probe_log_write_failure_uses_output_exit_code(self):
        self.fake_client()
        output = self.root / "probe-write-failure"
        real_private_write = ENVIRONMENT_MODULE.private_write

        def fail_probe_log(path, content):
            if Path(path).name == "client-tool.stdout.log":
                raise OSError("simulated probe log write failure")
            return real_private_write(path, content)

        with mock.patch.object(
            ENVIRONMENT_MODULE, "private_write", side_effect=fail_probe_log
        ):
            with redirect_stderr(io.StringIO()) as stderr:
                exit_code = ENVIRONMENT_MODULE.main(
                    [
                        "--client",
                        str(self.client),
                        "--benchmark",
                        str(self.benchmark),
                        "--timeout",
                        "0.2",
                        "--output",
                        str(output),
                    ]
                )
        self.assertEqual(exit_code, 2)
        self.assertIn("simulated probe log write failure", stderr.getvalue())
        self.assertFalse((output / "result.json").exists())

    def test_result_write_failure_uses_output_exit_code(self):
        output = self.root / "write-failure"
        with (
            mock.patch.object(
                ENVIRONMENT_MODULE,
                "run_checks",
                return_value={"status": "ready"},
            ),
            mock.patch.object(
                ENVIRONMENT_MODULE,
                "private_write",
                side_effect=OSError("simulated result write failure"),
            ),
        ):
            with redirect_stderr(io.StringIO()) as stderr:
                exit_code = ENVIRONMENT_MODULE.main(["--output", str(output)])
        self.assertEqual(exit_code, 2)
        self.assertIn("simulated result write failure", stderr.getvalue())
        self.assertFalse((output / "result.json").exists())
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    test_result = unittest.TextTestRunner(verbosity=2).run(suite)
    results = (
        Path(__file__).resolve().parents[1] / "cases" / "contract" / "results"
    )
    results.mkdir(parents=True, exist_ok=True)
    report = {
        "success": test_result.wasSuccessful(),
        "tests": test_result.testsRun,
        "failures": len(test_result.failures),
        "errors": len(test_result.errors),
        "requires_live_service": False,
        "semantic_checks": [
            "ready/blocked/unknown and series exit codes in help/output",
            "wrong_version/missing_tool/auth/timeout/correctness",
            "secret redaction, raw numeric semantics and nondefault endpoints",
            "config/default/cli account alignment and user_source evidence",
            "probe and result output-write failures use exit code 2",
            "password-example session cleanup on success and failure",
            "exclusive output preservation",
            "bounded timeout/cancellation process and PID cleanup",
        ],
    }
    (results / "result.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    sys.exit(0 if test_result.wasSuccessful() else 1)
