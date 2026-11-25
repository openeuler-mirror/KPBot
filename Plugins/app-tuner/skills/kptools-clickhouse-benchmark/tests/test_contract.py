#!/usr/bin/env python3
"""Offline contract regressions for the bounded ClickHouse benchmark."""

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

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_benchmark.py"
SPEC = importlib.util.spec_from_file_location("clickhouse_benchmark", SCRIPT)
BENCHMARK_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK_MODULE)
REAL_NATIVE_OUTPUT = """Loaded 1 queries.

Queries executed: 3 (100%).

127.0.0.1:19000, queries: 3, QPS: 23.854, RPS: 23853.576, MiB/s: 0.182, \
result RPS: 23.854, result MiB/s: 0.000.

0%\t\t0.001 sec.\t
10%\t\t0.001 sec.\t
20%\t\t0.001 sec.\t
30%\t\t0.001 sec.\t
40%\t\t0.001 sec.\t
50%\t\t0.001 sec.\t
60%\t\t0.001 sec.\t
70%\t\t0.001 sec.\t
80%\t\t0.002 sec.\t
90%\t\t0.002 sec.\t
95%\t\t0.002 sec.\t
99%\t\t0.002 sec.\t
99.9%\t\t0.002 sec.\t
99.99%\t\t0.002 sec.\t
"""
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
config = json.loads((root / "fake-config.json").read_text(encoding="utf-8"))
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
is_workload = "--iterations" in args
state_path = root / "workload-state"
workload_index = int(state_path.read_text()) if state_path.exists() else 0
if is_workload:
    workload_index += 1
    state_path.write_text(str(workload_index), encoding="utf-8")
    if config.get("sleep_at") == workload_index:
        time.sleep(10)
    if config.get("fail_at") == workload_index:
        print("simulated native runtime failure", file=sys.stderr)
        raise SystemExit(9)
    if config.get("malformed_at") == workload_index:
        print("unrecognized native report")
        raise SystemExit(0)
    iterations = int(args[args.index("--iterations") + 1])
    qps_values = config["qps"]
    qps = qps_values[min(workload_index - 1, len(qps_values) - 1)]
    print("Loaded 1 queries.")
    print(f"Queries executed: {iterations} (100%).")
    print(
        f"localhost:9000, queries: {iterations}, QPS: {qps:.3f}, "
        f"RPS: {qps * 1000:.3f}, MiB/s: 0.182, "
        f"result RPS: {qps:.3f}, result MiB/s: 0.000."
    )
    for percentile, seconds in (
        ("0", 0.001),
        ("50", 0.001),
        ("95", 0.002),
        ("99", 0.002),
        ("99.9", 0.002),
        ("99.99", 0.002),
    ):
        print(f"{percentile}%      {seconds:.3f} sec.")
    raise SystemExit(0)
if config.get("auth_error") and query == "SELECT version()":
    print(
        "Code: 516. AUTHENTICATION_FAILED password="
        + os.environ.get("CLICKHOUSE_PASSWORD", ""),
        file=sys.stderr,
    )
    raise SystemExit(1)
if query == "SELECT version()":
    print(config["server_version"])
    raise SystemExit(0)
if query.startswith("SELECT sum(number) FROM numbers("):
    rows = int(query.removeprefix("SELECT sum(number) FROM numbers(")[:-1])
    expected = rows * (rows - 1) // 2
    wrong = config.get("wrong_aggregate")
    wrong_after = config.get("wrong_after_workload")
    if wrong or (wrong_after and workload_index >= wrong_after):
        expected += 1
    print(expected)
    raise SystemExit(0)
print("unexpected query", file=sys.stderr)
raise SystemExit(3)
"""
DEFAULT_CONFIG = {
    "tool_version": "26.8.42.13",
    "server_version": "26.8.42.13",
    "qps": [999, 10, 20, 30],
    "help_flags": [
        "--query",
        "--iterations",
        "--concurrency",
        "--delay",
        "--client-side-time",
        "--config",
    ],
}


class BenchmarkContractTests(unittest.TestCase):
    """Run the actual CLI with fake tools; these are not service E2E tests."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "fake-clickhouse"
        self.benchmark = self.root / "fake-clickhouse-benchmark"
        self.secret = "benchmark-contract-secret"
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

    def invoke(
        self,
        output,
        benchmark=None,
        extra=(),
        bounded=True,
        single=False,
    ):
        command = [
            sys.executable,
            str(SCRIPT),
            "--client",
            str(self.client),
            "--output",
            str(output),
        ]
        if single:
            command.append("--single-binary")
        else:
            command += ["--benchmark", str(benchmark or self.benchmark)]
        if bounded:
            command += [
                "--timeout",
                "0.2",
                "--rows",
                "1000",
                "--iterations",
                "3",
                "--warmup-iterations",
                "2",
                "--rounds",
                "3",
            ]
        command += list(map(str, extra))
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
            "0=success",
            "1=preflight/workload failure",
            "2=argument/output error or collision",
            "3=unknown (reserved)",
            "130=interrupted",
        ):
            self.assertIn(fragment, completed.stdout)

    def test_exact_clickhouse_268_native_output(self):
        metrics = BENCHMARK_MODULE.parse_native_metrics(
            REAL_NATIVE_OUTPUT, expected_queries=3
        )
        self.assertEqual(metrics["query_count"], 3)
        self.assertEqual(metrics["qps"], 23.854)
        self.assertEqual(metrics["read_rows_per_second"], 23853.576)
        self.assertEqual(metrics["read_mib_per_second"], 0.182)
        self.assertEqual(metrics["result_rows_per_second"], 23.854)
        self.assertEqual(metrics["latency_ms"], {"p50": 1, "p95": 2, "p99": 2})

    def test_success_excludes_warmup_and_reports_native_metrics(self):
        self.fake_client()
        output = self.root / "nested" / "success"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["preflight"]["status"], "ready")
        self.assertEqual(
            result["environment"]["benchmark_version"], "unavailable"
        )
        self.assertEqual(result["warmup"]["native"]["qps"], 999)
        self.assertFalse(result["warmup"]["included_in_summary"])
        self.assertEqual(len(result["rounds"]), 3)
        self.assertEqual(
            [item["native"]["qps"] for item in result["rounds"]],
            [10, 20, 30],
        )
        self.assertEqual(result["summary"]["median_qps"], 20)
        self.assertNotEqual(result["summary"]["median_qps"], 264.75)
        self.assertEqual(result["summary"]["total_native_queries"], 9)
        self.assertEqual(result["summary"]["round_count"], 3)
        self.assertEqual(
            result["summary"]["median_native_latency_ms"]["p95"], 2
        )
        for phase in [result["warmup"], *result["rounds"]]:
            self.assertTrue(phase["correctness"]["passed"])
            self.assertEqual(phase["correctness"]["actual"], "499500")
            self.assertGreater(phase["wall_time_ms"], 0)
            self.assertEqual(
                phase["native"]["query_count"],
                2 if phase["phase"] == "warmup" else 3,
            )
            self.assertEqual(phase["native"]["read_mib_per_second"], 0.182)
        self.assertIn("not TPC-H", result["scope"])

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
                self.assertIn("scripts/run_benchmark.py", observed["argv"])
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
        self.assertIsNone(result["target"]["user"])
        self.assertEqual(result["target"]["user_source"], "config")
        query_calls = [call for call in self.calls() if "--query" in call]
        self.assertTrue(query_calls)
        for call in query_calls:
            self.assertNotIn("--user", call)

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
        workload = next(
            call for call in self.calls() if "--iterations" in call
        )
        self.assertEqual(workload[workload.index("--user") + 1], "cli-user")

    def test_omitted_user_without_config_uses_client_default(self):
        self.fake_client()
        output = self.root / "default-user"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.result(output)
        self.assertIsNone(result["target"]["user"])
        self.assertEqual(result["target"]["user_source"], "client_default")
        query_calls = [call for call in self.calls() if "--query" in call]
        self.assertTrue(query_calls)
        for call in query_calls:
            self.assertNotIn("--user", call)

    def test_secure_target_and_password_redaction(self):
        self.fake_client()
        config = self.root / "secure.xml"
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
                "reader",
                "--secure",
                "--config",
                config,
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        argv_text = "\n".join("\n".join(call) for call in self.calls())
        all_evidence = (
            argv_text
            + completed.stdout
            + "\n".join(
                path.read_text(encoding="utf-8")
                for path in output.glob("*.log")
            )
        )
        self.assertNotIn(self.secret, all_evidence)
        self.assertNotIn("--password", argv_text)
        workload = next(
            call for call in self.calls() if "--iterations" in call
        )
        self.assertIn("db.example", workload)
        self.assertIn("9440", workload)
        self.assertIn("--secure", workload)
        self.assertIn(str(config), workload)

    def test_unsupported_server_version_stops_before_workload(self):
        self.fake_client(server_version="22.7.9.1")
        output = self.root / "old"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "wrong_version")
        self.assertIsNone(result["warmup"])
        self.assertFalse(any("--iterations" in call for call in self.calls()))

    def test_missing_benchmark_tool_preserves_failed_result(self):
        self.fake_client()
        output = self.root / "missing"
        completed = self.invoke(output, benchmark=self.root / "not-installed")
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "missing_tool")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["warmup"])

    def test_auth_failure_is_classified_and_redacted(self):
        self.fake_client(auth_error=True)
        output = self.root / "auth"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "auth")
        evidence = completed.stdout + "\n".join(
            path.read_text(encoding="utf-8") for path in output.glob("*.log")
        )
        self.assertNotIn(self.secret, evidence)
        self.assertFalse(any("--iterations" in call for call in self.calls()))

    def test_timeout_kills_round_and_preserves_warmup(self):
        self.fake_client(sleep_at=2)
        output = self.root / "timeout"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "timeout")
        self.assertEqual(result["warmup"]["status"], "passed")
        self.assertEqual(len(result["rounds"]), 1)
        self.assertTrue(result["rounds"][0]["timed_out"])
        self.assertIsNone(result["rounds"][0]["correctness"])
        self.assertIsNone(result["summary"])
        self.assertTrue((output / "round-1.stderr.log").exists())

    def test_timeout_kills_sigterm_ignoring_forked_descendant(self):
        started = time.monotonic()
        outcome = BENCHMARK_MODULE.run_process(
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
        real_popen = BENCHMARK_MODULE.subprocess.Popen
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
            BENCHMARK_MODULE.subprocess, "Popen", side_effect=popen
        ):
            with self.assertRaises(KeyboardInterrupt):
                BENCHMARK_MODULE.run_process(
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

    def test_runtime_failure_stops_later_rounds_and_keeps_logs(self):
        self.fake_client(fail_at=3)
        output = self.root / "runtime"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["warmup"]["status"], "passed")
        self.assertEqual(result["rounds"][0]["status"], "passed")
        self.assertEqual(result["rounds"][1]["returncode"], 9)
        self.assertEqual(len(result["rounds"]), 2)
        self.assertIn(
            "simulated native runtime failure",
            (output / "round-2.stderr.log").read_text(encoding="utf-8"),
        )
        self.assertFalse((output / "round-3.stdout.log").exists())
        self.assertIsNone(result["summary"])

    def test_exact_correctness_failure_invalidates_native_success(self):
        self.fake_client(wrong_after_workload=1)
        output = self.root / "correctness"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "correctness")
        self.assertIsNotNone(result["warmup"]["native"])
        self.assertFalse(result["warmup"]["correctness"]["passed"])
        self.assertEqual(result["warmup"]["correctness"]["actual"], "499501")
        self.assertEqual(result["rounds"], [])
        self.assertIsNone(result["summary"])

    def test_preflight_correctness_failure_never_starts_workload(self):
        self.fake_client(wrong_aggregate=True)
        output = self.root / "preflight-correctness"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.result(output)["failure"]["kind"], "correctness")
        self.assertFalse(any("--iterations" in call for call in self.calls()))

    def test_changed_native_output_fails_closed(self):
        self.fake_client(malformed_at=1)
        output = self.root / "metrics"
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 1)
        result = self.result(output)
        self.assertEqual(result["failure"]["kind"], "metric_parse")
        self.assertTrue(result["warmup"]["correctness"]["passed"])
        self.assertIsNone(result["summary"])

    def test_output_collision_preserves_old_evidence(self):
        self.fake_client()
        output = self.root / "collision"
        output.mkdir()
        sentinel = output / "result.json"
        sentinel.write_text("previous result", encoding="utf-8")
        completed = self.invoke(output)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "previous result"
        )
        self.assertEqual(self.calls(), [])

    def test_result_write_failure_uses_output_exit_code(self):
        self.fake_client()
        output = self.root / "write-failure"
        real_private_write = BENCHMARK_MODULE.private_write

        def fail_result_write(path, content):
            if Path(path).name == "result.json":
                raise OSError("simulated result write failure")
            return real_private_write(path, content)

        with mock.patch.object(
            BENCHMARK_MODULE, "private_write", side_effect=fail_result_write
        ):
            with redirect_stderr(io.StringIO()) as stderr:
                exit_code = BENCHMARK_MODULE.main(
                    [
                        "--client",
                        str(self.client),
                        "--benchmark",
                        str(self.benchmark),
                        "--timeout",
                        "0.2",
                        "--rows",
                        "1000",
                        "--iterations",
                        "3",
                        "--warmup-iterations",
                        "2",
                        "--rounds",
                        "3",
                        "--output",
                        str(output),
                    ]
                )
        self.assertEqual(exit_code, 2)
        self.assertIn("simulated result write failure", stderr.getvalue())
        self.assertFalse((output / "result.json").exists())
        self.assertTrue(any("--iterations" in call for call in self.calls()))

    def test_unbounded_or_excessive_arguments_are_rejected_before_output(self):
        invalid = (
            ("--rounds", "2"),
            ("--iterations", "0"),
            ("--timeout", "nan"),
            ("--concurrency", "17"),
            ("--rows", "999"),
        )
        for index, arguments in enumerate(invalid):
            with self.subTest(arguments=arguments):
                self.fake_client()
                output = self.root / f"invalid-{index}"
                completed = self.invoke(output, extra=arguments, bounded=False)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(output.exists())
                self.assertEqual(self.calls(), [])
                (self.root / "calls.jsonl").unlink(missing_ok=True)

    def test_single_binary_layout_is_self_contained(self):
        self.fake_client()
        output = self.root / "single"
        completed = self.invoke(output, single=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls()
        self.assertTrue(any(call[0] == "client" for call in calls))
        self.assertTrue(any(call[0] == "benchmark" for call in calls))
        version_calls = [call for call in calls if "--version" in call]
        self.assertEqual(version_calls, [["--version"]])
        result = self.result(output)
        self.assertEqual(result["summary"]["median_qps"], 20)
        self.assertEqual(result["environment"]["client_version"], "26.8.42.13")
        self.assertEqual(
            result["environment"]["benchmark_version"], "26.8.42.13"
        )
        self.assertEqual(result["environment"]["server_version"], "26.8.42.13")


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
            "warmup exclusion and native units/counts",
            "exact per-phase correctness",
            "wrong_version/missing_tool/auth/timeout/runtime/metric failures",
            "series exit codes for success/failure/arguments/output/help",
            "secret redaction, bounded arguments, exclusive output",
            "password-example session cleanup on success and failure",
            "single-binary independence",
            "bounded timeout/cancellation process and PID cleanup",
        ],
    }
    (results / "result.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    sys.exit(0 if test_result.wasSuccessful() else 1)
