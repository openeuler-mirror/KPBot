"""One-shot backend CLI tests using async fakes; no server or SDK calls.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_backend_cli -v
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from kpbot_memory import backend_cli, cli  # noqa: E402


def trusted():
    return {"backend": "openviking", "user_id": "local-user", "current_project": "p1",
            "allowed_projects": ["p1", "p2"], "options": {
                "api_key": "TOP-SECRET-CONFIG", "account_id": "account", "user_id": "provider-user"}}


def ready(state="ready"):
    return {"ok": True, "data": {"state": state, "message": "", "supports_shared": False}}


def error(code="UNAVAILABLE", source_id=None):
    return {"ok": False, "error": {"code": code, "message": "backend fault", "source_id": source_id}}


def record(kind="experience"):
    return {"kind": kind, "project_id": "p1", "task_id": "task-1",
            "content": {"conditions": ["原条件"], "conclusion": "原结论", "evidence": ["原证据"]}}


class CliBackend:
    def __init__(self):
        self.calls = []
        self.hooks = {}
        self.replies = {}

    async def invoke(self, name, payload=None, timeout=None):
        self.calls.append((name, copy.deepcopy(payload), timeout))
        if name in self.hooks:
            value = self.hooks[name](payload, timeout)
            return await value if inspect.isawaitable(value) else value
        if name in self.replies:
            return copy.deepcopy(self.replies[name])
        if name in ("initialize", "status"):
            return ready()
        if name in ("store_experience", "store_process"):
            return {"ok": True, "data": {"source_id": "original-locator", "processing": "pending"}}
        if name == "search":
            return {"ok": True, "data": {"items": [], "partial": False}}
        if name == "close":
            return None
        raise AssertionError(name)

    async def initialize(self, options, *, timeout=30.0):
        return await self.invoke("initialize", options, timeout)

    async def status(self, *, timeout=30.0):
        return await self.invoke("status", timeout=timeout)

    async def store_experience(self, payload, *, timeout=30.0):
        return await self.invoke("store_experience", payload, timeout)

    async def store_process(self, payload, *, timeout=30.0):
        return await self.invoke("store_process", payload, timeout)

    async def search(self, payload, *, timeout=30.0):
        return await self.invoke("search", payload, timeout)

    async def close(self):
        return await self.invoke("close")


class BackendCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "trusted backend.json"
        self.write_config(trusted())
        self.backend = CliBackend()
        self.factory = lambda: self.backend

    def write_config(self, value, mode=0o600):
        self.config_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.config_path.chmod(mode)

    def input(self, value):
        path = self.root / "complete input.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def raw_input(self, data):
        path = self.root / "complete input.json"
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return str(path)

    def run_command(self, arguments, *, stdin=b"", use_config=True):
        command = ["backend", *arguments]
        if use_config:
            command.extend(["--config", str(self.config_path)])
        stdout, stderr = io.StringIO(), io.StringIO()
        input_stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with mock.patch.object(backend_cli, "create_backend", side_effect=self.factory) as factory:
            with mock.patch.object(sys, "stdin", input_stream), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = cli.main(command)
        output = stdout.getvalue()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(output.splitlines()), 1)
        result = json.loads(output)
        self.assertNotIn("TOP-SECRET-CONFIG", output)
        return status, result, factory.call_count

    def assert_error(self, status, result, code):
        self.assertEqual(status, 1)
        self.assertEqual(set(result), {"ok", "error"})
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message", "source_id"})
        self.assertEqual(result["error"]["code"], code)

    def names(self):
        return [name for name, _, _ in self.backend.calls]

    def test_initialize_is_single_probe_then_cleanup_no_extra_status(self):
        status, result, count = self.run_command(["initialize"])
        self.assertEqual(status, 0)
        self.assertEqual(result, ready())
        self.assertEqual(count, 1)
        self.assertEqual(self.names(), ["initialize", "close"])
        options = self.backend.calls[0][1]
        self.assertEqual(options["owner_id"], "local-user")
        self.assertEqual(options["user_id"], "provider-user")
        self.assertNotIn("owner_id", json.loads(self.config_path.read_text())["options"])

    def test_status_bootstraps_runs_status_once_and_closes(self):
        status, result, _ = self.run_command(["status"])
        self.assertEqual(status, 0)
        self.assertEqual(result, ready())
        self.assertEqual(self.names(), ["initialize", "status", "close"])

    def test_close_only_releases_this_command_client_and_is_null(self):
        status, result, _ = self.run_command(["close"])
        self.assertEqual(status, 0)
        self.assertEqual(result, {"ok": True, "data": None})
        self.assertEqual(self.names(), ["initialize", "close"])

    def test_remember_json_preserves_record_and_routes_each_kind(self):
        for kind, method in (("experience", "store_experience"), ("task_process", "store_process")):
            self.backend = CliBackend()
            payload = record(kind)
            path = self.input(payload)
            before = Path(path).read_bytes()
            status, result, _ = self.run_command(["remember", "--input", path])
            self.assertEqual(status, 0)
            self.assertEqual(result["data"], {"source_id": "original-locator", "processing": "pending"})
            self.assertEqual(self.names(), ["initialize", method, "close"])
            self.assertEqual(self.backend.calls[1][1], {**payload, "owner_id": "local-user", "visibility": "private"})
            self.assertEqual(Path(path).read_bytes(), before)

    def test_text_submission_keeps_bom_newlines_whitespace_and_unicode(self):
        text = "\ufeff用户输入\r\n执行记录\n工具结果\t\n最终结果  \r\n"
        path = self.root / "complete task process.txt"
        path.write_bytes(text.encode("utf-8"))
        status, result, _ = self.run_command(["remember", "--content-file", str(path), "--kind", "task_process",
                                             "--project-id", "p2", "--task-id", "full-task"])
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.names(), ["initialize", "store_process", "close"])
        self.assertEqual(self.backend.calls[1][1], {"kind": "task_process", "project_id": "p2", "task_id": "full-task",
                                                   "owner_id": "local-user", "visibility": "private", "content": text})
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))

    def test_text_and_json_stdin_are_explicitly_supported(self):
        status, result, _ = self.run_command(["recall", "--input", "-"], stdin='{"features":["寄存器压力"]}'.encode("utf-8"))
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.backend.calls[1][1]["features"], ["寄存器压力"])
        self.backend = CliBackend()
        status, result, _ = self.run_command(["remember", "--content-file", "-", "--kind", "task_process",
                                             "--project-id", "p1", "--task-id", "t"], stdin="全文\r\n".encode("utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(self.backend.calls[1][1]["content"], "全文\r\n")

    def test_recall_multiple_projects_returns_plain_new_result(self):
        status, result, _ = self.run_command(["recall", "--input", self.input({"query": "原查询", "features": ["feature"],
                                                                             "project_ids": ["p1", "p2"], "kinds": ["experience"]})])
        self.assertEqual(status, 0)
        self.assertEqual(result, {"ok": True, "data": {"items": [], "partial": False}})
        self.assertEqual(self.names(), ["initialize", "search", "close"])
        self.assertEqual(self.backend.calls[1][1]["sources"], [{"project_id": "p1", "owner_id": "local-user"},
                                                             {"project_id": "p2", "owner_id": "local-user"}])
        self.assertNotIn("advice", result)
        self.assertNotIn("degraded", result)

    def test_backend_cli_does_not_require_a_bearer_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status, result, _ = self.run_command(["initialize"])
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])

    def test_configuration_mode_and_owner_scope_failure_are_visible(self):
        self.write_config(trusted(), mode=0o644)
        status, result, count = self.run_command(["initialize"])
        self.assert_error(status, result, "ACCESS_DENIED")
        self.assertEqual(count, 0)
        self.write_config({**trusted(), "options": {**trusted()["options"], "owner_id": "other-user"}})
        status, result, count = self.run_command(["initialize"])
        self.assert_error(status, result, "ACCESS_DENIED")
        self.assertEqual(count, 1)
        self.assertEqual(self.names(), ["close"])

    def test_invalid_input_strict_json_rejected_before_client_creation(self):
        cases = ['{"query":"TOP-SECRET-INPUT","query":"duplicate"}', '{"query":NaN}', '{"query":Infinity}',
                 '{"query":"\\ud800"}', '[]', 'null', '{} {}', b"\xff", '{"query":']
        for raw in cases:
            with self.subTest(raw=type(raw)):
                status, result, count = self.run_command(["recall", "--input", self.raw_input(raw)])
                self.assert_error(status, result, "INVALID_ARGUMENT")
                self.assertEqual(count, 0)
                self.assertNotIn("TOP-SECRET-INPUT", str(result))

    def test_missing_text_or_json_file_is_safe_error(self):
        status, result, count = self.run_command(["recall", "--input", str(self.root / "TOP-SECRET-NONEXISTENT")])
        self.assert_error(status, result, "INVALID_ARGUMENT")
        self.assertEqual(count, 0)
        self.assertNotIn("TOP-SECRET-NONEXISTENT", str(result))

    def test_text_invalid_utf8_is_not_replaced(self):
        status, result, count = self.run_command(["remember", "--content-file", self.raw_input(b"\xff"),
                                                 "--kind", "task_process", "--project-id", "p1", "--task-id", "t"])
        self.assert_error(status, result, "INVALID_ARGUMENT")
        self.assertEqual(count, 0)

    def test_business_input_and_text_flags_are_validated_for_each_operation(self):
        path = self.input(record())
        cases = [["remember"], ["recall"], ["status", "--input", path], ["initialize", "--kind", "task_process"],
                 ["close", "--content-file", path], ["remember", "--content-file", path],
                 ["remember", "--content-file", path, "--kind", "task_process", "--project-id", "p1"],
                 ["recall", "--content-file", path, "--kind", "task_process", "--project-id", "p1", "--task-id", "t"],
                 ["remember", "--input", path, "--task-id", "ignored"],
                 ["remember", "--input", path, "--content-file", path]]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                status, result, count = self.run_command(arguments)
                self.assert_error(status, result, "INVALID_ARGUMENT")
                self.assertEqual(count, 0)

    def test_parser_errors_are_structured_exit_one_and_do_not_echo_values(self):
        cases = [[], ["TOP-SECRET-ARG"], ["status", "--unknown=TOP-SECRET-ARG"],
                 ["status", "--timeout", "TOP-SECRET-ARG"], ["status", "--timeout"],
                 ["remember", "--kind", "experience"]]
        cases += [["status", "--timeout", value] for value in ("nan", "inf", "0", "-1")]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                status, result, count = self.run_command(arguments)
                self.assert_error(status, result, "INVALID_ARGUMENT")
                self.assertEqual(count, 0)
                self.assertNotIn("TOP-SECRET-ARG", str(result))
        status, result, count = self.run_command(["status"], use_config=False)
        self.assert_error(status, result, "INVALID_ARGUMENT")
        self.assertEqual(count, 0)

    def test_configurable_input_byte_limit_rejects_without_truncating_or_initializing(self):
        config = trusted()
        config["options"]["max_input_bytes"] = 10
        self.write_config(config)
        path = self.raw_input("中文全文中文全文")
        before = Path(path).read_bytes()
        status, result, count = self.run_command(["remember", "--content-file", path, "--kind", "task_process",
                                                 "--project-id", "p1", "--task-id", "t"])
        self.assert_error(status, result, "TOO_LARGE")
        self.assertIn("10", result["error"]["message"])
        self.assertEqual(count, 0)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_default_input_limit_is_independent_and_can_be_explicitly_raised(self):
        content = "x" * (backend_cli.DEFAULT_MAX_INPUT_BYTES + 1)
        path = self.raw_input(content)
        args = ["remember", "--content-file", path, "--kind", "task_process", "--project-id", "p1", "--task-id", "t"]
        status, result, count = self.run_command(args)
        self.assert_error(status, result, "TOO_LARGE")
        self.assertEqual(count, 0)
        config = trusted()
        config["options"]["max_input_bytes"] = 2 * backend_cli.DEFAULT_MAX_INPUT_BYTES
        self.write_config(config)
        status, result, count = self.run_command(args)
        self.assertEqual(status, 0)
        self.assertEqual(count, 1)
        self.assertEqual(self.backend.calls[1][1]["content"], content)

    def test_input_limit_rejects_invalid_configuration_before_creating_client(self):
        for limit in (True, 0, -1, None, "1024", 10 ** 100):
            config = trusted()
            config["options"]["max_input_bytes"] = limit
            self.write_config(config)
            status, result, count = self.run_command(["initialize"])
            self.assert_error(status, result, "INVALID_ARGUMENT")
            self.assertEqual(count, 0)

    def test_bootstrap_failure_and_non_ready_success_do_not_execute_operation(self):
        for reply, expected in ((error("UNAVAILABLE"), "UNAVAILABLE"), (ready("unavailable"), "UNAVAILABLE")):
            self.backend = CliBackend()
            self.backend.replies["initialize"] = reply
            status, result, _ = self.run_command(["recall", "--input", self.input({"query": "q"})])
            self.assert_error(status, result, expected)
            self.assertEqual(self.names(), ["initialize", "close"])

    def test_backend_error_is_not_degraded_and_finally_close_preserves_source(self):
        expected = error("WRITE_UNKNOWN", "already-allocated")
        self.backend.replies["store_experience"] = expected
        self.backend.replies["close"] = error("BACKEND_ERROR")
        status, result, _ = self.run_command(["remember", "--input", self.input(record())])
        self.assert_error(status, result, "WRITE_UNKNOWN")
        self.assertEqual(result, expected)
        self.assertEqual(self.names(), ["initialize", "store_experience", "close"])

    def test_cleanup_failure_replaces_only_a_successful_primary_result(self):
        self.backend.replies["close"] = error("BACKEND_ERROR")
        status, result, _ = self.run_command(["initialize"])
        self.assert_error(status, result, "BACKEND_ERROR")
        self.assertEqual(self.names(), ["initialize", "close"])

    def test_confirmed_save_source_survives_cleanup_failure(self):
        self.backend.replies["close"] = error("BACKEND_ERROR")
        status, result, _ = self.run_command(["remember", "--input", self.input(record())])
        self.assert_error(status, result, "BACKEND_ERROR")
        self.assertEqual(result["error"]["source_id"], "original-locator")
        self.assertEqual(self.names(), ["initialize", "store_experience", "close"])

    def test_unexpected_backend_exception_never_prints_input_or_credential(self):
        def fail(payload, timeout):
            raise RuntimeError("TOP-SECRET-CONFIG TOP-SECRET-INPUT")

        self.backend.hooks["search"] = fail
        status, result, _ = self.run_command(["recall", "--input", self.input({"query": "TOP-SECRET-INPUT"})])
        self.assert_error(status, result, "BACKEND_ERROR")
        self.assertNotIn("TOP-SECRET-INPUT", str(result))
        self.assertEqual(self.names(), ["initialize", "search", "close"])

    def test_started_write_timeout_preserves_cancel_locator_and_has_one_save(self):
        async def allocated(payload, timeout):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                exc.source_id = "viking://allocated-original"
                raise

        self.backend.hooks["store_process"] = allocated
        status, result, _ = self.run_command(["remember", "--input", self.input(record("task_process")), "--timeout", "0.01"])
        self.assert_error(status, result, "WRITE_UNKNOWN")
        self.assertEqual(result["error"]["source_id"], "viking://allocated-original")
        self.assertEqual(self.names(), ["initialize", "store_process", "close"])

    def test_read_timeout_is_visible_and_client_is_closed(self):
        async def hang(payload, timeout):
            await asyncio.Event().wait()

        self.backend.hooks["search"] = hang
        status, result, _ = self.run_command(["recall", "--input", self.input({"query": "q"}), "--timeout", "0.01"])
        self.assert_error(status, result, "TIMEOUT")
        self.assertEqual(self.names(), ["initialize", "search", "close"])

    def test_operation_uses_remaining_bootstrap_deadline_not_a_new_timeout(self):
        async def slow_initialize(payload, timeout):
            await asyncio.sleep(0.01)
            return ready()

        self.backend.hooks["initialize"] = slow_initialize
        status, result, _ = self.run_command(["status", "--timeout", "0.1"])
        self.assertEqual(status, 0)
        self.assertLess(self.backend.calls[1][2], self.backend.calls[0][2])
        self.assertEqual(self.names(), ["initialize", "status", "close"])

    def test_input_ownership_sharing_and_project_rejections_are_not_swallowed(self):
        for changes, code in (({"owner_id": "other"}, "ACCESS_DENIED"), ({"project_id": "other"}, "ACCESS_DENIED"),
                              ({"visibility": "shared"}, "UNSUPPORTED")):
            self.backend = CliBackend()
            status, result, _ = self.run_command(["remember", "--input", self.input({**record(), **changes})])
            self.assert_error(status, result, code)
            self.assertEqual(self.names(), ["initialize", "close"])

    def test_fresh_facade_and_backend_are_created_for_each_invocation(self):
        instances = []

        def factory():
            backend = CliBackend()
            instances.append(backend)
            return backend

        self.factory = factory
        for _ in range(2):
            status, result, count = self.run_command(["initialize"])
            self.assertEqual(status, 0)
            self.assertEqual(count, 1)
        self.assertEqual(len(instances), 2)
        self.assertIsNot(instances[0], instances[1])
        self.assertEqual([[name for name, _, _ in instance.calls] for instance in instances],
                         [["initialize", "close"], ["initialize", "close"]])

    def test_unknown_top_level_command_prints_usage_and_exits_two(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = cli.main(["call", "status", "--input", "old.json"])
        self.assertEqual(status, 2)
        self.assertIn("kpbot memory backend", stderr.getvalue())

    def test_help_explains_one_shot_lifecycle_without_creating_backend(self):
        stdout = io.StringIO()
        with mock.patch.object(backend_cli, "create_backend") as factory, contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["backend", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("no cross-process state", stdout.getvalue())
        self.assertIn("not the service", stdout.getvalue())
        self.assertIn("--content-file", stdout.getvalue())
        factory.assert_not_called()
        self.assertIn("kpbot memory backend", backend_cli.build_parser().format_help())

    def test_actual_script_help_and_bad_args_need_no_sdk_or_service(self):
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        script = SCRIPTS / "kpbot-memory.py"
        help_run = subprocess.run([sys.executable, str(script), "backend", "--help"], env=environment,
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("no cross-process state", help_run.stdout)
        self.assertEqual(help_run.stderr, "")
        bad = subprocess.run([sys.executable, str(script), "backend", "TOP-SECRET-ARG"], env=environment,
                             capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)["error"]["code"], "INVALID_ARGUMENT")
        self.assertNotIn("TOP-SECRET-ARG", bad.stdout + bad.stderr)


class BackendSessionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_write_session_still_closes_and_retains_allocated_source(self):
        backend = CliBackend()
        entered = asyncio.Event()

        async def store(payload, timeout):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                exc.source_id = "allocated-process-source"
                raise

        backend.hooks["store_process"] = store
        with mock.patch.object(backend_cli, "create_backend", return_value=backend):
            task = asyncio.create_task(backend_cli._execute(trusted(), "remember", record("task_process"), 30))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            result = await task
        self.assertEqual(result["error"]["code"], "WRITE_UNKNOWN")
        self.assertEqual(result["error"]["source_id"], "allocated-process-source")
        self.assertEqual([name for name, _, _ in backend.calls], ["initialize", "store_process", "close"])

    async def test_cancelled_read_session_returns_error_and_closes(self):
        backend = CliBackend()
        entered = asyncio.Event()

        async def search(payload, timeout):
            entered.set()
            await asyncio.Event().wait()

        backend.hooks["search"] = search
        with mock.patch.object(backend_cli, "create_backend", return_value=backend):
            task = asyncio.create_task(backend_cli._execute(trusted(), "recall", {"query": "q"}, 30))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            result = await task
        self.assertEqual(result["error"]["code"], "BACKEND_ERROR")
        self.assertEqual([name for name, _, _ in backend.calls], ["initialize", "search", "close"])


if __name__ == "__main__":
    unittest.main()
