#!/usr/bin/env python3
"""Executable Tier-A contracts for the Doris environment skill (mocks only)."""

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
SCRIPT = SKILL_DIR / "scripts/check_environment.py"
SPEC = importlib.util.spec_from_file_location("doris_environment", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EnvironmentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "nested/report.json"
        self.calls = self.root / "calls.jsonl"

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
            with Path({env_path!r}).open("a", encoding="utf-8") as log:
                log.write(json.dumps({{
                    "HOME": os.environ.get("HOME"),
                    "MYSQL_TEST_LOGIN_FILE": os.environ.get(
                        "MYSQL_TEST_LOGIN_FILE"
                    ),
                }}) + "\\n")
            for arg in sys.argv:
                if arg.startswith("--defaults-file="):
                    Path({snapshot_path!r}).write_text(
                        Path(arg.split("=", 1)[1]).read_text(encoding="utf-8")
                    )
            if "--version" in sys.argv:
                if scenario == "wrong-client":
                    print("unrelated-tool 1.0")
                else:
                    print("mysql Ver 8.0.36 for Linux on x86_64")
                raise SystemExit(0)
            sql = sys.argv[-1]
            if scenario == "auth" and sql == "SELECT 1":
                secret = os.environ.get("MYSQL_PWD", "")
                sys.stderr.write("ERROR 1045 Access denied password=" + secret)
                raise SystemExit(1)
            if scenario == "runtime" and sql == "SELECT 1":
                sys.stderr.write("ERROR 1105 synthetic runtime failure")
                raise SystemExit(1)
            if scenario == "timeout" and sql == "SELECT 1":
                time.sleep(5)
            show_headers = "--skip-column-names" not in sys.argv
            if sql == "SELECT 1":
                if show_headers:
                    print("1")
                print("2" if scenario == "wrong-select" else "1")
            elif sql == "SHOW FRONTENDS":
                if show_headers:
                    print("Name\\tJoin\\tAlive\\tVersion\\tCurrentConnected")
                version = "doris-3.0.8-rc01-contract"
                if scenario == "unsupported":
                    version = "doris-2.0.0-rc01-contract"
                print("fe\\ttrue\\ttrue\\t" + version + "\\tYes")
            elif sql == "SHOW BACKENDS":
                if show_headers:
                    print("BackendId\\tAlive\\tSystemDecommissioned\\tStatus")
                alive = "false" if scenario == "no-backend" else "true"
                status = "{{\\"isQueryDisabled\\":false}}"
                if scenario == "long-backends":
                    status = (
                        "{{\\"isQueryDisabled\\":false,\\"padding\\":\\""
                        + "x" * 5000 + "\\"}}"
                    )
                    print("1\\ttrue\\tfalse\\t" + status)
                    print("2\\ttrue\\tfalse\\t" + status)
                    raise SystemExit(0)
                elif scenario == "status-null":
                    status = "null"
                elif scenario == "status-array":
                    status = "[]"
                print("1\\t" + alive + "\\tfalse\\t" + status)
            elif sql.startswith("SELECT SUM(number)"):
                if show_headers:
                    print("SUM(number)\\tCOUNT(*)")
                if scenario == "bad-capability":
                    print("0\\t0")
                else:
                    print("499500\\t1000")
            else:
                raise SystemExit(2)
            """
        ).format(
            scenario=scenario,
            calls_path=str(self.calls),
            env_path=str(self.root / "client-env.jsonl"),
            snapshot_path=str(self.root / "sanitized.cnf"),
        )
        path.write_text(program, encoding="utf-8")
        path.chmod(0o755)
        return path

    def invoke(self, client, output=None, extra=(), env=None, timeout=0.5):
        command = [
            sys.executable,
            str(SCRIPT),
            "--client",
            str(client),
            "--output",
            str(output or self.output),
            "--timeout",
            str(timeout),
            *extra,
        ]
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
        return json.loads(self.output.read_text(encoding="utf-8"))

    def probe(self, result, name):
        return next(item for item in result["probes"] if item["name"] == name)

    def test_success_is_semantic_and_tcp_only(self):
        client = self.make_client()
        completed = self.invoke(client)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.read_result()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["probes"]), 7)
        self.assertTrue(
            all(item["status"] == "ready" for item in result["probes"])
        )
        frontend = self.probe(result, "frontend_compatibility")
        self.assertEqual(frontend["evidence"]["parsed_version"], "3.0.8")
        calls = [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]
        sql_calls = [call for call in calls if "--execute" in call]
        self.assertEqual(len(sql_calls), 4)
        for call in sql_calls:
            self.assertIn("--protocol=TCP", call)
            self.assertIn("--no-defaults", call)
            self.assertNotIn("--socket", call)
            self.assertFalse(any(arg.startswith("--password") for arg in call))
            sql = call[-1]
            if sql.startswith("SHOW "):
                self.assertNotIn("--skip-column-names", call)
            else:
                self.assertIn("--skip-column-names", call)
        capability = self.probe(result, "numbers_capability")
        self.assertEqual(capability["evidence"]["numbers_sum"], 499500)

    def test_nondefault_endpoint_and_secure_defaults_file(self):
        client = self.make_client()
        config = self.root / "client.cnf"
        config.write_text(
            "[client]\npassword=contract-secret\n", encoding="utf-8"
        )
        config.chmod(0o600)
        completed = self.invoke(
            client,
            extra=(
                "--host",
                "db.contract",
                "--port",
                "19030",
                "--user",
                "bench",
                "--config",
                str(config),
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.read_result()
        self.assertEqual(result["target"]["host"], "db.contract")
        self.assertEqual(result["target"]["port"], 19030)
        serialized = json.dumps(result)
        self.assertNotIn("contract-secret", serialized)
        calls = [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]
        forwarded = next(
            arg for arg in calls[1] if arg.startswith("--defaults-file=")
        )
        self.assertNotEqual(forwarded, "--defaults-file=" + str(config))
        sanitized = forwarded.split("=", 1)[1]
        self.assertEqual(
            (self.root / "sanitized.cnf").read_text(encoding="utf-8"),
            "[client]\npassword=contract-secret\n",
        )
        self.assertFalse(Path(sanitized).exists())
        self.assertIn("19030", calls[1])
        self.assertIn("db.contract", calls[1])

    def test_unsupported_doris_version_is_blocked(self):
        completed = self.invoke(self.make_client("unsupported"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["status"], "blocked")
        probe = self.probe(result, "frontend_compatibility")
        self.assertEqual(probe["reason"], "unsupported_version")

    def test_missing_tool_writes_failure_result(self):
        completed = self.invoke(self.root / "absent-mysql")
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        reason = self.probe(result, "client")["reason"]
        self.assertEqual(reason, "missing_tool")
        self.assertEqual(len(result["probes"]), 3)

    def test_authentication_failure_is_redacted(self):
        secret = "contract-password-never-log"
        completed = self.invoke(
            self.make_client("auth"), env={"DORIS_PASSWORD": secret}
        )
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        probe = self.probe(result, "query_authentication")
        self.assertEqual(probe["reason"], "authentication")
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, completed.stdout + completed.stderr)
        skipped = [item for item in result["probes"] if item["status"] == "skipped"]
        self.assertEqual(len(skipped), 3)
        self.assertTrue(
            all(item["reason"] == "dependency_skipped" for item in skipped)
        )
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["summary"]["skipped"], 3)

    def test_timeout_kills_client_and_preserves_evidence(self):
        started = time.monotonic()
        completed = self.invoke(self.make_client("timeout"), timeout=1.0)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        probe = self.probe(result, "query_authentication")
        self.assertEqual(probe["reason"], "timeout")
        self.assertTrue(probe["evidence"]["timed_out"])

    def test_runtime_failure_is_not_ready(self):
        completed = self.invoke(self.make_client("runtime"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            self.probe(result, "query_authentication")["reason"],
            "runtime_error",
        )

    def test_unexpected_select_result_is_unknown(self):
        completed = self.invoke(self.make_client("wrong-select"))
        self.assertEqual(completed.returncode, 3)
        result = self.read_result()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            self.probe(result, "query_authentication")["reason"],
            "inconclusive_result",
        )

    def test_no_alive_backend_is_blocked(self):
        completed = self.invoke(self.make_client("no-backend"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(
            self.probe(result, "backend_prerequisites")["reason"],
            "invalid_prerequisite",
        )

    def test_numbers_capability_failure_blocks_benchmark_readiness(self):
        completed = self.invoke(self.make_client("bad-capability"))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        capability = self.probe(result, "numbers_capability")
        self.assertEqual(capability["status"], "blocked")
        self.assertEqual(capability["reason"], "invalid_prerequisite")

    def test_output_collision_does_not_change_existing_file(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("keep-me", encoding="utf-8")
        completed = self.invoke(self.make_client())
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep-me")
        self.assertFalse(self.calls.exists())

    def test_insecure_config_blocks_before_client_execution(self):
        client = self.make_client()
        config = self.root / "world-readable.cnf"
        config.write_text("[client]\npassword=do-not-use\n", encoding="utf-8")
        config.chmod(0o644)
        completed = self.invoke(client, extra=("--config", str(config)))
        self.assertEqual(completed.returncode, 1)
        result = self.read_result()
        self.assertEqual(
            self.probe(result, "credential_source")["reason"],
            "insecure_config",
        )
        self.assertFalse(self.calls.exists())

    def test_long_backend_output_is_parsed_before_evidence_clipping(self):
        completed = self.invoke(self.make_client("long-backends"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.read_result()
        backend = self.probe(result, "backend_prerequisites")
        self.assertEqual(backend["status"], "ready")
        self.assertEqual(backend["evidence"]["backend_count"], 2)
        self.assertEqual(backend["evidence"]["query_capable_backend_count"], 2)
        self.assertLessEqual(len(backend["evidence"]["stdout"]), 4110)
        self.assertIn("[TRUNCATED]", backend["evidence"]["stdout"])

    def test_non_object_backend_status_is_reported_without_crash(self):
        for scenario in ("status-null", "status-array"):
            with self.subTest(scenario=scenario):
                self.output = self.root / (scenario + ".json")
                completed = self.invoke(self.make_client(scenario))
                self.assertEqual(completed.returncode, 3, completed.stderr)
                result = self.read_result()
                backend = self.probe(result, "backend_prerequisites")
                self.assertEqual(backend["status"], "unknown")
                self.assertEqual(backend["reason"], "inconclusive_result")
                self.assertIn("must be an object", backend["evidence"]["detail"])

    def test_implicit_login_files_are_isolated_from_real_home(self):
        real_home = self.root / "real-home"
        real_home.mkdir()
        (real_home / ".mylogin.cnf").write_text("saved-secret", encoding="utf-8")
        completed = self.invoke(
            self.make_client(), env={"HOME": str(real_home)}
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        environments = [
            json.loads(line)
            for line in (self.root / "client-env.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(environments), 5)
        for environment in environments:
            self.assertNotEqual(environment["HOME"], str(real_home))
            login = Path(environment["MYSQL_TEST_LOGIN_FILE"])
            self.assertEqual(login.parent, Path(environment["HOME"]))
            self.assertFalse(login.exists())

    def test_unsafe_or_unrelated_defaults_options_fail_closed(self):
        bodies = (
            "[client]\ninit-command=DROP TABLE secret_table\n",
            "[client]\n!include /tmp/other.cnf\n",
            "[client]\n!includedir /tmp/other.d\n",
            "[client]\nlocal-infile=1\n",
        )
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                config = self.root / ("forbidden-%d.cnf" % index)
                config.write_text(body, encoding="utf-8")
                config.chmod(0o600)
                self.output = self.root / ("forbidden-%d.json" % index)
                completed = self.invoke(
                    self.make_client(), extra=("--config", str(config))
                )
                self.assertEqual(completed.returncode, 1)
                result = self.read_result()
                credential = self.probe(result, "credential_source")
                self.assertEqual(credential["reason"], "insecure_config")
                self.assertNotIn("secret_table", json.dumps(result))
                self.assertFalse(self.calls.exists())

    def test_wrong_client_is_not_accepted(self):
        completed = self.invoke(self.make_client("wrong-client"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            self.probe(self.read_result(), "client")["reason"],
            "unsupported_client",
        )

    def test_version_and_timeout_boundaries(self):
        for version in ("2.1.9", "3.0.8", "3.1.2"):
            text = (
                "Join\tAlive\tVersion\tCurrentConnected\n"
                "true\ttrue\tdoris-" + version + "-test\tYes\n"
            )
            self.assertEqual(
                MODULE.parse_frontends(text)["parsed_version"], version
            )
        bad = (
            "Join\tAlive\tVersion\tCurrentConnected\n"
            "true\ttrue\tdoris-4.0.0\tYes\n"
        )
        with self.assertRaises(MODULE.UnsupportedVersion):
            MODULE.parse_frontends(bad)
        for value in ("nan", "inf", "0", "301"):
            with self.assertRaises(Exception):
                MODULE.finite_seconds(value)

    def test_keyboard_interrupt_kills_owned_group_and_bounded_drains(self):
        events = []

        class FakeProcess:
            pid = 43210

            def communicate(self, timeout=None):
                events.append(timeout)
                if len(events) == 1:
                    raise KeyboardInterrupt
                return "", ""

        args = MODULE.build_parser().parse_args(
            ["--output", str(self.output), "--timeout", "0.25"]
        )
        runner = MODULE.ProbeRunner(args)
        process = FakeProcess()

        def record_kill(pid, sig):
            events.append("kill")

        with (
            patch.object(MODULE.subprocess, "Popen", return_value=process),
            patch.object(MODULE.os, "killpg", side_effect=record_kill) as kill,
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.execute(["mysql"], 0.25)
        kill.assert_called_once_with(process.pid, MODULE.signal.SIGKILL)
        self.assertEqual(events, [0.25, "kill", 1.0])

    def test_redaction_handles_environment_and_key_value_secrets(self):
        text = MODULE.redact("token secret password=hunter2", ("secret",))
        self.assertNotIn("secret", text)
        self.assertNotIn("hunter2", text)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        EnvironmentContractTests
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
