#!/usr/bin/env python3
"""Executable Tier-A contracts for Doris benchmark behavior (mocks only)."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parents[2]
SCRIPT = SKILL_DIR / "scripts/run_benchmark.py"
SPEC = importlib.util.spec_from_file_location("doris_benchmark", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "nested/run-001"
        self.calls = self.root / "calls.jsonl"
        self.query_count = self.root / "query-count"

    def make_client(self, scenario="success"):
        path = self.root / ("mysql-" + scenario)
        program = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys
            import time

            scenario = {scenario!r}
            with Path({calls_path!r}).open("a", encoding="utf-8") as log:
                log.write(json.dumps(sys.argv) + "\\n")
            if "--version" in sys.argv:
                print("mysql Ver 8.0.36 for Linux on x86_64")
                raise SystemExit(0)
            sql = sys.argv[-1]
            if scenario == "auth" and sql == "SELECT 1":
                secret = os.environ.get("MYSQL_PWD", "")
                sys.stderr.write("ERROR 1045 Access denied password=" + secret)
                raise SystemExit(1)
            show_headers = "--skip-column-names" not in sys.argv
            if sql == "SELECT 1":
                if show_headers:
                    print("1")
                print("1")
            elif sql == "SHOW FRONTENDS":
                if show_headers:
                    print("Name\\tJoin\\tAlive\\tVersion\\tCurrentConnected")
                version = "doris-3.0.8-rc01-contract"
                if scenario == "unsupported":
                    version = "doris-4.0.0-rc01-contract"
                print("fe\\ttrue\\ttrue\\t" + version + "\\tYes")
            elif sql == "SHOW BACKENDS":
                if show_headers:
                    print("BackendId\\tAlive\\tSystemDecommissioned\\tStatus")
                print("1\\ttrue\\tfalse\\t{{\\"isQueryDisabled\\":false}}")
            elif sql.startswith("SELECT SUM(number)"):
                rows = int(sql.rsplit('="', 1)[1].split('")', 1)[0])
                counter = Path({query_count_path!r})
                count = 0
                if counter.exists():
                    count = int(counter.read_text() or "0")
                counter.write_text(str(count + 1))
                if show_headers:
                    print("SUM(number)\\tCOUNT(*)")
                if scenario == "bad-capability" and rows == 1000:
                    print("0\\t0")
                    raise SystemExit(0)
                if rows != 1000:
                    if scenario == "timeout":
                        time.sleep(5)
                    if scenario == "runtime":
                        sys.stderr.write(
                            "ERROR 1105 synthetic workload failure"
                        )
                        raise SystemExit(1)
                expected = rows * (rows - 1) // 2
                if scenario == "correctness" and rows != 1000:
                    expected += 1
                print(str(expected) + "\\t" + str(rows))
            else:
                raise SystemExit(2)
            """
        ).format(
            scenario=scenario,
            calls_path=str(self.calls),
            query_count_path=str(self.query_count),
        )
        path.write_text(program, encoding="utf-8")
        path.chmod(0o755)
        return path

    def invoke(
        self,
        client,
        output=None,
        extra=(),
        env=None,
        timeout=0.4,
        include_output=True,
    ):
        command = [
            sys.executable,
            str(SCRIPT),
            "--client",
            str(client),
            "--timeout",
            str(timeout),
            "--queries-per-round",
            "2",
            "--warmup",
            "1",
            *extra,
        ]
        if include_output:
            command.extend(("--output", str(output or self.output)))
        child_env = os.environ.copy()
        child_env.pop("DORIS_PASSWORD", None)
        child_env.pop("MYSQL_PWD", None)
        if env:
            child_env.update(env)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            env=child_env,
            check=False,
        )

    def read_result(self):
        return json.loads(
            (self.output / "result.json").read_text(encoding="utf-8")
        )

    def read_raw(self):
        path = self.output / "queries.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_success_has_real_samples_rounds_and_exact_counts(self):
        completed = self.invoke(self.make_client())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.read_result()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["success"])
        self.assertFalse(result["is_tpch"])
        self.assertEqual(len(result["rounds"]), 3)
        self.assertEqual(result["summary"]["query_count"], 6)
        self.assertEqual(result["summary"]["timed_round_count"], 3)
        self.assertFalse(result["summary"]["warmup_included"])
        self.assertEqual(result["warmup"]["queries_completed"], 1)
        self.assertGreater(
            result["summary"]["throughput"]["queries_per_second"], 0
        )
        raw = self.read_raw()
        self.assertEqual(len(raw), 7)
        self.assertEqual(
            [item["phase"] for item in raw], ["warmup"] + ["timed"] * 6
        )
        self.assertTrue(
            all(item["correctness"]["exact_match"] for item in raw)
        )
        self.assertTrue(all(item["elapsed_ns"] > 0 for item in raw))
        self.assertEqual(result["summary"]["query_latency"]["count"], 6)
        self.assertEqual(
            result["workload"]["generated_rows_per_query"],
            MODULE.DEFAULT_ROWS,
        )
        self.assertEqual(
            result["workload"]["expected_sum_per_query"],
            MODULE.expected_sum(MODULE.DEFAULT_ROWS),
        )
        for item in result["rounds"]:
            self.assertEqual(item["query_count"], 2)
            self.assertEqual(item["query_latency"]["count"], 2)
            native = item["validated_native_results"]
            self.assertEqual(
                native["sum_per_query"],
                MODULE.expected_sum(MODULE.DEFAULT_ROWS),
            )
            self.assertEqual(native["count_per_query"], MODULE.DEFAULT_ROWS)
        calls = [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]
        for call in calls:
            if "--execute" not in call:
                continue
            if call[-1].startswith("SHOW "):
                self.assertNotIn("--skip-column-names", call)
            else:
                self.assertIn("--skip-column-names", call)
        capability = next(
            item
            for item in result["preflight"]
            if item["name"] == "numbers_capability"
        )
        self.assertEqual(capability["evidence"]["numbers_sum"], 499500)

    def test_warmup_failure_never_enters_timed_rounds(self):
        completed = self.invoke(self.make_client("correctness"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["phase"], "warmup")
        self.assertEqual(result["error"]["reason"], "correctness_mismatch")
        self.assertEqual(result["rounds"], [])
        self.assertIsNone(result["summary"])
        raw = self.read_raw()
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["status"], "failed")
        expected = "{}\t{}".format(
            MODULE.expected_sum(MODULE.DEFAULT_ROWS) + 1,
            MODULE.DEFAULT_ROWS,
        )
        self.assertEqual(raw[0]["correctness"]["observed_tsv"], expected)

    def test_runtime_failure_preserves_failure_json_and_raw_log(self):
        completed = self.invoke(self.make_client("runtime"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["reason"], "runtime_error")
        raw = self.read_raw()
        self.assertEqual(len(raw), 1)
        self.assertIn("synthetic workload failure", raw[0]["stderr"])

    def test_workload_timeout_is_bounded_and_preserved(self):
        started = time.monotonic()
        completed = self.invoke(self.make_client("timeout"), timeout=0.2)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["error"]["reason"], "timeout")
        raw = self.read_raw()
        self.assertTrue(raw[0]["timed_out"])
        self.assertEqual(raw[0]["returncode"], -9)

    def test_authentication_failure_is_preflight_and_redacted(self):
        secret = "benchmark-secret-never-log"
        completed = self.invoke(
            self.make_client("auth"), env={"DORIS_PASSWORD": secret}
        )
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["error"]["phase"], "preflight")
        auth = next(
            item
            for item in result["preflight"]
            if item["name"] == "query_authentication"
        )
        self.assertEqual(auth["reason"], "authentication")
        self.assertFalse((self.output / "queries.jsonl").exists())
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, completed.stdout + completed.stderr)

    def test_unsupported_version_fails_before_workload(self):
        completed = self.invoke(self.make_client("unsupported"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        frontend = next(
            item
            for item in result["preflight"]
            if item["name"] == "frontend_compatibility"
        )
        self.assertEqual(frontend["reason"], "unsupported_version")
        self.assertFalse((self.output / "queries.jsonl").exists())

    def test_capability_failure_blocks_before_timed_workload(self):
        completed = self.invoke(self.make_client("bad-capability"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        capability = next(
            item
            for item in result["preflight"]
            if item["name"] == "numbers_capability"
        )
        self.assertEqual(capability["status"], "blocked")
        self.assertFalse((self.output / "queries.jsonl").exists())

    def test_custom_rows_drive_sql_and_exact_native_results(self):
        completed = self.invoke(self.make_client(), extra=("--rows", "1000"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.read_result()
        self.assertEqual(result["workload"]["generated_rows_per_query"], 1000)
        self.assertEqual(result["workload"]["expected_sum_per_query"], 499500)
        for record in self.read_raw():
            self.assertEqual(record["generated_rows"], 1000)
            self.assertEqual(
                record["correctness"]["observed_tsv"], "499500\t1000"
            )

    def test_missing_tool_fails_closed_with_result(self):
        completed = self.invoke(self.root / "missing-client")
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["preflight"][-1]["reason"], "missing_tool")
        self.assertFalse(result["success"])

    def test_output_collision_preserves_existing_directory(self):
        self.output.mkdir(parents=True)
        sentinel = self.output / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        completed = self.invoke(self.make_client())
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse(self.calls.exists())

    def test_rounds_below_three_and_unbounded_values_are_rejected(self):
        for extra in (
            ("--rounds", "2"),
            ("--rounds", "21"),
            ("--queries-per-round", "101"),
            ("--warmup", "0"),
            ("--timeout", "inf"),
            ("--rows", "0"),
            ("--rows", "100000001"),
        ):
            output = self.root / ("invalid-" + extra[1])
            completed = self.invoke(
                self.make_client(), output=output, extra=extra
            )
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(output.exists())

    def test_defaults_have_three_timed_rounds_and_one_warmup(self):
        args = MODULE.build_parser().parse_args(["--output", "new-output"])
        self.assertEqual(args.rounds, 3)
        self.assertEqual(args.queries_per_round, 5)
        self.assertEqual(args.warmup, 1)
        self.assertEqual(args.rows, 1_000_000)
        self.assertEqual(args.timeout, 10.0)

    def test_no_arbitrary_sql_option_or_shell_interpolation(self):
        completed = self.invoke(
            self.make_client(),
            extra=("--output", str(self.output), "--execute", "DROP TABLE x"),
            include_output=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.output.exists())
        sql = MODULE.benchmark_sql(1000)
        self.assertIn("SELECT SUM(number), COUNT(*)", sql)
        self.assertIn('numbers("number"="1000")', sql)
        self.assertNotIn("{", sql)

    def test_secure_config_and_nondefault_endpoint_are_forwarded(self):
        client = self.make_client()
        config = self.root / "secure.cnf"
        config.write_text("[client]\npassword=file-secret\n", encoding="utf-8")
        config.chmod(0o600)
        completed = self.invoke(
            client,
            extra=(
                "--host",
                "doris.contract",
                "--port",
                "19030",
                "--user",
                "bench",
                "--config",
                str(config),
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        serialized = (self.output / "result.json").read_text(encoding="utf-8")
        self.assertNotIn("file-secret", serialized)
        calls = [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("--defaults-file=" + str(config), calls[1])
        self.assertIn("doris.contract", calls[1])
        self.assertIn("19030", calls[1])
        self.assertFalse(
            any(arg.startswith("--password") for call in calls for arg in call)
        )

    def test_insecure_config_prevents_any_client_execution(self):
        config = self.root / "public.cnf"
        config.write_text("[client]\npassword=unused\n", encoding="utf-8")
        config.chmod(0o644)
        completed = self.invoke(
            self.make_client(), extra=("--config", str(config))
        )
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["preflight"][0]["reason"], "insecure_config")
        self.assertFalse(self.calls.exists())

    def test_keyboard_interrupt_kills_owned_group_and_bounded_drains(self):
        events = []

        class FakeProcess:
            pid = 43211

            def communicate(self, timeout=None):
                events.append(timeout)
                if len(events) == 1:
                    raise KeyboardInterrupt
                return "", ""

        args = MODULE.build_parser().parse_args(
            ["--output", str(self.output), "--timeout", "0.25"]
        )
        client = MODULE.DorisClient(args)
        process = FakeProcess()

        def record_kill(pid, sig):
            events.append("kill")

        with (
            patch.object(MODULE.subprocess, "Popen", return_value=process),
            patch.object(MODULE.os, "killpg", side_effect=record_kill) as kill,
        ):
            with self.assertRaises(KeyboardInterrupt):
                client.execute(["mysql"])
        kill.assert_called_once_with(process.pid, MODULE.signal.SIGKILL)
        self.assertEqual(events, [0.25, "kill", 1.0])

    def test_percentile_and_metric_units_are_deterministic(self):
        records = [
            {"elapsed_ns": value, "status": "passed"}
            for value in (4_000_000, 1_000_000, 3_000_000, 2_000_000)
        ]
        metrics = MODULE.latency_metrics(records)
        self.assertEqual(metrics["unit"], "ms")
        self.assertEqual(metrics["min"], 1.0)
        self.assertEqual(metrics["p50"], 2.0)
        self.assertEqual(metrics["p95"], 4.0)
        self.assertEqual(metrics["mean"], 2.5)
        round_result = MODULE.round_metrics(1, records, 20_000_000, 1000)
        throughput = round_result["throughput"]["queries_per_second"]
        self.assertEqual(throughput, 200.0)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        BenchmarkContractTests
    )
    test_result = unittest.TextTestRunner(verbosity=2).run(suite)
    results_dir = Path(
        os.environ.get(
            "CASE_RESULTS", str(Path(__file__).resolve().parent / "results")
        )
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "success": test_result.wasSuccessful(),
        "tests": test_result.testsRun,
        "failures": len(test_result.failures),
        "errors": len(test_result.errors),
        "requires_live_service": False,
        "mocks_only_contract_evidence": True,
    }
    (results_dir / "result.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    sys.exit(0 if test_result.wasSuccessful() else 1)
