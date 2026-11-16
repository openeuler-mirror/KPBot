"""Run: python3 -m unittest discover -s <skill>/tests -v."""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capture_runtime as pc
import capturectl
import pcap
import analyze


def config(path, **values):
    capturectl.write_config(path, {"out_dir": str(path.parent / "data"), **values})


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "capturectl.py"), *map(str, args)],
                          capture_output=True, text=True, timeout=15)


def wait_worker_count(path, expected, timeout=6):
    deadline = time.monotonic() + timeout
    while len(capturectl.active_state(path)["workers"]) != expected:
        if time.monotonic() >= deadline:
            raise AssertionError("expected workers did not publish their initial state")
        time.sleep(0.05)


@contextlib.contextmanager
def mock_process(*args):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_proxy.py"), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Fail clearly rather than hanging if a mock fails to bind/start.
        import select
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            raise AssertionError("mock startup timeout")
        line = proc.stdout.readline()
        if not line:
            raise AssertionError(proc.stderr.read())
        yield proc, int(line)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def request(port, stream=False, session="session-test"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = {"model": "mock", "stream": stream,
            "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
    try:
        connection.request("POST", "/v1/chat/completions", json.dumps(body),
                           {"x-session-id": session, "Authorization": "Bearer SECRET"})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


class ConfigTests(unittest.TestCase):
    def test_metadata_params_do_not_store_request_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            config(path, compress=False)
            writer = pc._CaptureWriter(str(path), str(path) + ".state", os.getpid())
            body = {
                "model": "mock", "stream": False, "temperature": 0.5, "max_tokens": 16,
                "messages": [{"role": "user", "content": "BODY_PRIVATE_SENTINEL"}],
                "chat_template": "TEMPLATE_PRIVATE_SENTINEL",
                "chat_template_kwargs": {"business": "KWARGS_PRIVATE_SENTINEL"},
                "response_format": {"type": "json_schema", "json_schema": {
                    "description": "SCHEMA_PRIVATE_SENTINEL"}},
                "custom": "CUSTOM_PRIVATE_SENTINEL" * 500,
                "stop": ["STOP_PRIVATE_SENTINEL"],
                "top_p": "INVALID_PRIVATE_SENTINEL",
                "max_completion_tokens": {"description": "INVALID_PRIVATE_SENTINEL"},
            }
            try:
                writer.handle(pc._cap_dumps({"ev": "req", "id": "meta", "ts": time.time(),
                                             "ep": "chat", "lvl": "meta", "out": "none"}),
                              pc._cap_dumps(body))
            finally:
                writer.close()
            records = list(pcap.iter_requests([str(path.parent / "data")]))
            self.assertEqual(writer.n["errors"], 0)
            self.assertEqual(len(records), 1)
            for segment in (path.parent / "data").rglob("*.jsonl"):
                self.assertNotIn("PRIVATE_SENTINEL", segment.read_text())
            self.assertEqual(records[0].params()["temperature"], 0.5)
            self.assertEqual(records[0].params()["max_tokens"], 16)
            self.assertNotIn("chat_template", records[0].params())
            self.assertIsNone(analyze.base_row(records[0], {})["has_stop"])
            for key in ("chat_template", "chat_template_kwargs", "response_format", "custom", "stop",
                        "top_p", "max_completion_tokens"):
                with self.subTest(key=key):
                    vb = pc._cap_dumps(body[key])
                    self.assertEqual(records[0].rec["param_meta"][key], {
                        "type": type(body[key]).__name__, "bytes": len(vb), "hash": pc._cap_h(vb)})

    def test_metadata_model_identifier_rejects_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            config(path, compress=False)
            writer = pc._CaptureWriter(str(path), str(path) + ".state", os.getpid())
            try:
                for index, model in enumerate(({"description": "MODEL_PRIVATE_SENTINEL"},
                                               ["MODEL_PRIVATE_SENTINEL"], None)):
                    writer.handle(pc._cap_dumps({"ev": "req", "id": str(index), "ts": time.time(),
                                                 "ep": "chat", "lvl": "meta", "out": "none"}),
                                  pc._cap_dumps({"model": model, "messages": []}))
            finally:
                writer.close()
            records = list(pcap.iter_requests([str(path.parent / "data")]))
            self.assertEqual(writer.n["errors"], 0)
            self.assertEqual(len(records), 3)
            for record in records:
                self.assertIsNone(record.rec["model"])
                self.assertNotIn("model", record.params())
                self.assertIn("model", record.rec["param_meta"])
            for segment in (path.parent / "data").rglob("*.jsonl"):
                self.assertNotIn("PRIVATE_SENTINEL", segment.read_text())

    def test_metadata_allowlist_checks_value_types(self):
        for key in ("max_tokens", "temperature", "stream", "tool_choice", "reasoning_effort"):
            for value in ("PRIVATE_SENTINEL", {"description": "PRIVATE_SENTINEL"},
                          ["PRIVATE_SENTINEL"]):
                with self.subTest(key=key, value=value):
                    self.assertFalse(pc._cap_meta_param_allowed(key, value))
        self.assertFalse(pc._cap_meta_param_allowed("temperature", float("inf")))
        self.assertFalse(pc._cap_meta_param_allowed("max_tokens", True))
        self.assertTrue(pc._cap_meta_param_allowed("tool_choice", "auto"))

    def test_text_record_preserves_inline_and_blob_params(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            config(path, compress=False)
            writer = pc._CaptureWriter(str(path), str(path) + ".state", os.getpid())
            body = {"model": "mock", "messages": [], "chat_template": "PRIVATE_SENTINEL",
                    "chat_template_kwargs": {"business": "PRIVATE_SENTINEL" * 500},
                    "response_format": {"description": "PRIVATE_SENTINEL"}}
            try:
                writer.handle(pc._cap_dumps({"ev": "req", "id": "text", "ts": time.time(),
                                             "ep": "chat", "lvl": "text", "out": "none"}),
                              pc._cap_dumps(body))
            finally:
                writer.close()
            records = list(pcap.iter_requests([str(path.parent / "data")]))
            self.assertEqual(writer.n["errors"], 0)
            self.assertEqual(records[0].body(), body)

    def test_case_verifier_accepts_real_analysis_and_rejects_changed_inputs(self):
        case = ROOT / "cases/agent-offline-boundary"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results").mkdir()
            def module(name):
                spec = importlib.util.spec_from_file_location(name, case / (name + ".py"))
                result = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(result)
                return result
            module("seed").seed(root, SCRIPTS)
            with_text = root / "results/sessions-with-text"
            result = subprocess.run([sys.executable, str(SCRIPTS / "session_view.py"),
                                     str(root / "input/data"), "--out", str(with_text), "--include-text"],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PRIVATE_CASE_SENTINEL", (with_text / "sessions.html").read_text())
            result = subprocess.run([sys.executable, str(SCRIPTS / "analyze.py"),
                                     str(root / "input/data"), "--out", str(root / "results/analysis")],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            (root / "results/REPORT.md").write_text("Synthetic analysis: four requests and two sessions; no tokenizer or logs.")
            (root / "results/trace.jsonl").write_text(json.dumps({"type": "tool_use", "part": {
                "tool": "skill", "state": {"input": {"name": "kptools-prompt-capture"}}}}) + "\n")
            verifier = module("verify")
            with patch.dict(os.environ, {"SKILL_MODE": "with", "AGENT_EXIT_CODE": "0"}):
                self.assertTrue(verifier.verify(root)["success"])
                (root / "input/capture.json").write_text("{}")
                self.assertFalse(verifier.verify(root)["success"])

    def test_ab_prompts_only_differ_in_explicit_invocation(self):
        prompts = ROOT / "cases/agent-offline-boundary/prompts"
        with_skill = (prompts / "with-skill.md").read_text().split("\n\n", 1)[1]
        self.assertEqual(with_skill, (prompts / "without-skill.md").read_text())

    def test_truncated_segment_and_budget_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            config(path, compress=False)
            writer = pc._CaptureWriter(str(path), str(path) + ".state", os.getpid())
            writer.budget_ok = False
            writer.handle(pc._cap_dumps({"ev": "req"}), b"{}")
            self.assertEqual(writer.n["drop_budget"], 1)
            writer.close()
            segment = Path(directory) / "seg-truncated.jsonl"
            segment.write_text('{"t":"h","v":1}\n{"t":"r"')
            self.assertEqual(list(pcap.iter_requests([str(segment)])), [])

    def test_defaults_validation_and_credentials(self):
        cfg = pc.capture_validate({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["text_rate"], 0)
        self.assertFalse(cfg["follow_gold"])
        for bad in ({"unknown": 1}, {"text_rate": float("nan")}, {"follow_new": 1.2},
                    {"keep_headers": ["Authorization"]}, {"session_headers": ["Cookie"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pc.capture_validate(bad)

    def test_control_requires_duration_and_does_not_claim_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            self.assertEqual(cli("--config", path, "init").returncode, 0)
            self.assertNotEqual(cli("--config", path, "init").returncode, 0)
            self.assertEqual(cli("--config", path, "on").returncode, 2)
            self.assertEqual(cli("--config", path, "on", "--duration", "nan").returncode, 2)
            result = cli("--config", path, "--timeout", "0", "on", "--duration", "1")
            self.assertEqual(result.returncode, 4)
            self.assertEqual(json.loads(result.stdout)["applied"], "no_workers")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_automatic_remote_code_retry(self):
        calls = []

        def load(*args, **kwargs):
            calls.append(kwargs)
            raise ValueError("requires remote code")

        module = types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=load))
        with patch.dict(sys.modules, {"transformers": module}), self.assertRaises(ValueError):
            pcap.Renderer("mock")
        self.assertEqual(calls, [{"trust_remote_code": False}])

    def test_all_script_help(self):
        for name in ("analyze", "session_view", "join_vllm_log", "export_corpus",
                     "check_tokenizer", "dump_requests", "capturectl"):
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, str(SCRIPTS / (name + ".py")), "--help"],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "capture.json"

    async def asyncTearDown(self):
        pc.capture_shutdown()
        self.temp.cleanup()

    def begin(self, session="session-test", client="opencode"):
        body = {"model": "mock", "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
        return pc.begin("/chat/completions", body, json.dumps(body).encode(),
                        [(b"x-session-id", session.encode()), (b"authorization", b"SECRET"),
                         (b"user-agent", client.encode())])

    async def test_follow_list_reload_replaces_old_sessions(self):
        values = {"enabled": True, "stop_at_unix": time.time() + 20, "text_rate": 0,
                  "follow_sessions": ["A"]}
        config(self.path, **values)
        pc.start(str(self.path))
        first = self.begin("A")
        self.assertEqual(first.lvl, "text")
        first.finish("ok")
        config(self.path, **{**values, "follow_sessions": ["B"]})
        pc._capture_rt._reload()
        for session, level in (("A", "meta"), ("B", "text")):
            ctx = self.begin(session)
            self.assertEqual(ctx.lvl, level)
            ctx.finish("ok")

    async def test_follow_prefix_removal_returns_to_metadata(self):
        values = {"enabled": True, "stop_at_unix": time.time() + 20, "text_rate": 0,
                  "follow_sessions": ["team-"]}
        config(self.path, **values)
        pc.start(str(self.path))
        ctx = self.begin("team-1")
        self.assertEqual(ctx.lvl, "text")
        ctx.finish("ok")
        config(self.path, **{**values, "follow_sessions": []})
        runtime = pc._capture_rt
        runtime._reload()
        await asyncio.sleep(0)
        self.assertEqual(runtime._followed, {})
        ctx = self.begin("team-1")
        self.assertEqual(ctx.lvl, "meta")
        ctx.finish("ok")

    async def test_follow_off_on_clears_cache_even_without_yield(self):
        values = {"enabled": True, "stop_at_unix": time.time() + 20,
                  "text_rate": 0, "follow_new": 1}
        config(self.path, **values)
        pc.start(str(self.path))
        runtime = pc._capture_rt
        ctx = self.begin("A")
        self.assertEqual(ctx.lvl, "text")
        ctx.finish("ok")
        old_cfg = runtime.cfg
        config(self.path, **{**values, "enabled": False})
        runtime._reload()
        # Both reloads can occur before queued event-loop callbacks run.
        config(self.path, **values)
        runtime._reload()
        ctx = self.begin("B")
        self.assertEqual(ctx.lvl, "text")
        ctx.finish("ok")
        runtime._adopt_follow({"A"}, old_cfg)
        ctx = self.begin("A")
        self.assertEqual(ctx.lvl, "meta")
        ctx.finish("ok")
        self.assertEqual(runtime._follow_auto, {"B"})

    async def test_follow_auto_scope_changes_and_unrelated_settings(self):
        values = {"enabled": True, "stop_at_unix": time.time() + 20,
                  "text_rate": 0, "follow_new": 2}
        config(self.path, **values)
        pc.start(str(self.path))
        runtime = pc._capture_rt
        for session in ("A", "B"):
            ctx = self.begin(session, client="curl/8")
            self.assertEqual(ctx.lvl, "text")
            ctx.finish("ok")
        config(self.path, **{**values, "follow_gold": True})
        runtime._reload()
        ctx = self.begin("A", client="curl/8")
        self.assertTrue(ctx.gold)
        ctx.finish("ok")
        self.assertEqual(runtime._follow_auto, {"A", "B"})
        values.update(follow_new=1, follow_clients=["opencode"])
        config(self.path, **values)
        runtime._reload()
        ctx = self.begin("A", client="curl/8")
        self.assertEqual(ctx.lvl, "meta")
        ctx.finish("ok")
        for session, level in (("C", "text"), ("B", "meta")):
            ctx = self.begin(session)
            self.assertEqual(ctx.lvl, level)
            ctx.finish("ok")
        self.assertEqual(runtime._follow_auto, {"C"})

    async def test_follow_sync_rejects_old_worker_versions_and_queued_adoptions(self):
        values = {"enabled": True, "stop_at_unix": time.time() + 20,
                  "text_rate": 0, "follow_new": 1}
        config(self.path, **values)
        pc.start(str(self.path))
        runtime = pc._capture_rt
        old_cfg = runtime.cfg
        state_dir = Path(runtime.state_dir)
        state_dir.mkdir(exist_ok=True)
        peer = state_dir / "worker-peer.json"
        pc._cap_atomic_json(str(peer), {"enabled": True, "updated": time.time(),
                                       "followed": ["A"], "follow_version": old_cfg.version})
        queued = []
        with patch.object(runtime, "_call", side_effect=lambda fn, *args: queued.append((fn, args))):
            runtime._sync_follow()
        self.assertTrue(queued)
        values.update(follow_clients=["opencode"])
        config(self.path, **values)
        runtime._reload()
        for fn, args in queued:
            fn(*args)
        self.assertNotIn("A", runtime._followed)
        queued.clear()
        with patch.object(runtime, "_call", side_effect=lambda fn, *args: queued.append((fn, args))):
            runtime._sync_follow()
        self.assertEqual(queued, [])
        pc._cap_atomic_json(str(peer), {"enabled": True, "updated": time.time(),
                                       "followed": ["B"], "follow_version": runtime.cfg.version})
        runtime._sync_follow()
        await asyncio.sleep(0)
        self.assertIn("B", runtime._followed)
        runtime._write_stats()
        state = json.loads((state_dir / f"worker-{os.getpid()}.json").read_text())
        self.assertEqual(state["follow_version"], runtime.cfg.version)
        self.assertNotIn("A", state["followed"])

    async def test_deadline_applies_without_controller_and_survives_restart(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 0.7, text_rate=1)
        pc.start(str(self.path))
        ctx = self.begin()
        self.assertIsNotNone(ctx)
        ctx.finish("ok")
        await asyncio.sleep(1)
        self.assertIsNone(self.begin())
        self.assertFalse(pc._capture_rt.cfg.enabled)
        pc.capture_shutdown()
        pc.start(str(self.path))
        self.assertIsNone(self.begin())

    async def test_invalid_reload_keeps_last_valid_and_missing_config_stops(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 20)
        pc.start(str(self.path))
        pc._cap_atomic_json(str(self.path), {"unknown": True})
        pc._capture_rt._reload()
        self.assertTrue(pc._capture_rt.cfg.enabled)
        self.assertTrue(pc._capture_rt.cfg_error)
        self.path.unlink()
        pc._capture_rt._reload()
        self.assertIsNone(self.begin())

    async def test_stream_faults_cancellation_and_idempotent_finish(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 20, text_rate=1)
        pc.start(str(self.path))
        ctx = self.begin()

        async def chunks():
            yield b"first"
            yield b"second"

        with patch.object(pc.CaptureCtx, "_keep", side_effect=RuntimeError("writer failure")):
            self.assertEqual([x async for x in ctx.wrap(chunks())], [b"first", b"second"])
        ctx.finish("duplicate")
        self.assertEqual(pc._capture_rt._inflight, 0)
        self.assertEqual(pc._capture_rt.c["hook_errors"], 2)
        cancelled = self.begin()

        async def cancel():
            yield b"first"
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            _ = [x async for x in cancelled.wrap(cancel())]
        self.assertTrue(cancelled.done)

    async def test_queue_limit_and_oversized_body(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 20,
               queue_max_mb=0.00001, max_body_mb=0.00001, text_rate=1)
        pc.start(str(self.path))
        self.assertIsNone(self.begin())
        self.assertGreater(pc._capture_rt.c["drop_queue"], 0)
        self.assertGreater(pc._capture_rt.c["too_large"], 0)

    async def test_large_output_tail_respects_total_memory_budget(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 20,
               queue_max_mb=0.01, max_output_kb=0.1, text_rate=1)
        pc.start(str(self.path))
        contexts = [self.begin() for _ in range(2)]
        for ctx in contexts:
            ctx.response_chunk(b"x" * 100000)
        runtime = pc._capture_rt
        self.assertLessEqual(runtime._buf_bytes + runtime._out_bytes, runtime.cfg.queue_max)
        for ctx in contexts:
            ctx.finish("ok")
        self.assertEqual(runtime._out_bytes, 0)

    async def test_pd_golden_round_trip_and_sampling(self):
        config(self.path, enabled=True, stop_at_unix=time.time() + 20, text_rate=1,
               follow_sessions=["session-test"], follow_gold=True, compress=True)
        pc.start(str(self.path))
        contexts = [self.begin() for _ in range(2)]
        self.assertTrue(all(ctx.gold for ctx in contexts))
        for ctx in contexts:
            self.assertEqual(ctx.prefill_extra(), {"return_token_ids": True, "return_prompt_text": True})
            ctx.prefill_start("rid-" + ctx.cid)
            value = {"usage": {"prompt_tokens": 2, "prompt_tokens_details": {"cached_tokens": 1}},
                     "prompt_token_ids": [1, 2], "prompt_text": "PRIVATE_SENTINEL"}
            ctx.prefill_done(types.SimpleNamespace(content=json.dumps(value).encode()), value, "P0")
            ctx.decode_started("D0")
            ctx.response_chunk(b'{"choices":[],"usage":{"completion_tokens":1}}')
            ctx.finish("ok")
        await asyncio.sleep(0.6)
        pc.capture_shutdown()
        records = list(pcap.iter_requests([str(self.path.parent / "data")]))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].gold_ids(), [1, 2])
        self.assertEqual(records[0].gold_text(), "PRIVATE_SENTINEL")
        for record in records:
            self.assertNotIn("authorization", record.headers())
        for segment in (self.path.parent / "data").rglob("*.gz"):
            self.assertEqual(segment.stat().st_mode & 0o777, 0o600)


class EndToEndTests(unittest.TestCase):
    def test_follow_list_hot_reload_across_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            values = {"enabled": True, "stop_at_unix": time.time() + 30,
                      "text_rate": 0, "follow_sessions": ["A"]}
            config(path, **values)
            with mock_process("--backend") as (_, backend_port):
                with mock_process("--backend-port", backend_port, "--capture-config", path) as (_, port1):
                    with mock_process("--backend-port", backend_port, "--capture-config", path) as (_, port2):
                        wait_worker_count(path, 2)
                        self.assertEqual(capturectl.wait_applied(path, 6), "ok")
                        for port in (port1, port2):
                            self.assertEqual(request(port, session="A")[0], 200)
                        config(path, **{**values, "follow_sessions": ["B"]})
                        self.assertEqual(capturectl.wait_applied(path, 6), "ok")
                        for port in (port1, port2):
                            for session in ("A", "B"):
                                self.assertEqual(request(port, session=session)[0], 200)
                        self.assertEqual(cli("--config", path, "off").returncode, 0)
                        end = time.monotonic() + 10
                        while time.monotonic() < end and capturectl.active_state(path)["writers"]:
                            time.sleep(0.2)
                        self.assertEqual(capturectl.active_state(path)["writers"], [])
            records = list(pcap.iter_requests([str(path.parent / "data")]))
            self.assertEqual([r.rec["lvl"] for r in records if r.rec["sess"] == "A"],
                             ["text", "text", "meta", "meta"])
            self.assertEqual([r.rec["lvl"] for r in records if r.rec["sess"] == "B"], ["text", "text"])

    def test_http_equivalence_workers_and_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            config(path)
            with mock_process("--backend") as (_, backend_port):
                with mock_process("--backend-port", backend_port) as (_, baseline_port):
                    expected = [request(baseline_port, stream) for stream in (False, True)]
                with mock_process("--backend-port", backend_port, "--capture-config", path) as (_, port1):
                    with mock_process("--backend-port", backend_port, "--capture-config", path) as (_, port2):
                        wait_worker_count(path, 2)
                        self.assertEqual([request(port1, s) for s in (False, True)], expected)
                        config(path, enabled=True, stop_at_unix=time.time() + 30, text_rate=1)
                        self.assertEqual(capturectl.wait_applied(path, 6), "ok")
                        self.assertEqual(len(capturectl.active_state(path)["workers"]), 2)
                        for port in (port1, port2):
                            self.assertEqual([request(port, s) for s in (False, True)], expected)
                        # A killed writer must not break response forwarding.
                        state = capturectl.active_state(path)
                        writers = [w.get("writer_pid") for w in state["workers"] if w.get("writer_pid")]
                        if writers:
                            os.kill(writers[0], signal.SIGKILL)
                            self.assertEqual(request(port1), expected[0])
                        result = cli("--config", path, "off")
                        self.assertEqual(result.returncode, 0, result.stderr)
                        end = time.monotonic() + 10
                        while time.monotonic() < end and capturectl.active_state(path)["writers"]:
                            time.sleep(0.2)
                        self.assertTrue(all(not w["enabled"] for w in capturectl.active_state(path)["workers"]))
                # Processes are stopped; tolerate a killed writer's missing records.
            data = Path(directory) / "data"
            records = list(pcap.iter_requests([str(data)]))
            self.assertGreaterEqual(len(records), 2)
            for name in ("analyze", "session_view"):
                out = Path(directory) / name
                result = subprocess.run([sys.executable, str(SCRIPTS / (name + ".py")),
                                         str(data), "--out", str(out)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(list(out.glob("*.html")))
                for artifact in out.iterdir():
                    if artifact.is_file():
                        self.assertNotIn("PRIVATE_SENTINEL", artifact.read_text(), str(artifact))
            export = Path(directory) / "requests.jsonl"
            result = subprocess.run([sys.executable, str(SCRIPTS / "export_corpus.py"), str(data),
                                     "--format", "requests", "--out", str(export)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PRIVATE_SENTINEL", export.read_text())


if __name__ == "__main__":
    unittest.main()
