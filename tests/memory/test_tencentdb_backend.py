"""TencentDB MemoryCore adapter tests: stdlib unittest, fake HTTP only, no service.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_tencentdb_backend -v
"""

import asyncio
import copy
import json
from pathlib import Path
import re
import sys
import unittest
import uuid

_SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kpbot_memory.southbound import SouthboundMemory


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return copy.deepcopy(self.data)


def envelope(data):
    return FakeResponse(200, {"code": 0, "message": "ok", "request_id": "req-1", "data": data})


def error_envelope(code=400, message="DO NOT EXPOSE: TOP-SECRET-TOKEN and full body"):
    return FakeResponse(code, {"code": code, "message": message, "request_id": "req-1"})


class FakeClient:
    """Minimal MemoryCore gateway: L0 storage, session query, token search."""

    def __init__(self, **kwargs):
        self.headers = kwargs.get("headers") or {}
        self.store = {}
        self.calls = []
        self.handlers = {}
        self.delays = {}
        self.closed = False
        self.close_count = 0
        self.after_add = None
        self.health = {"status": "ok", "version": "0.1.0", "uptime": 1,
                       "stores": {"vectorStore": True, "embeddingService": False,
                                  "ftsAvailable": True},
                       "services": {}}

    def _scope(self, body):
        return (body.get("team_id", "default"), body.get("agent_id", "default"),
                body.get("user_id", "default"))

    def _rows(self, body):
        sessions = self.store.setdefault(self._scope(body), {})
        return sessions

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        if self.delays.get(path):
            await asyncio.sleep(self.delays[path])
        if path in self.handlers:
            result = self.handlers[path](method, kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                return result
        if method == "GET" and path == "/health":
            return FakeResponse(200, self.health)
        body = kwargs.get("json") or {}
        sessions = self._rows(body)
        if path == "/v3/conversation/add":
            messages = body.get("messages")
            if (not isinstance(messages, list) or not 1 <= len(messages) <= 100
                    or any(not isinstance(message, dict)
                           or message.get("role") not in ("user", "assistant")
                           or not isinstance(message.get("content"), str)
                           or not 1 <= len(message["content"]) <= 8192
                           for message in messages)):
                return error_envelope(422)
            stored = sessions.setdefault(body.get("session_id", "default"), [])
            for message in messages:
                stored.append({"id": "msg-" + uuid.uuid4().hex, "role": message["role"],
                               "content": message["content"],
                               "timestamp": "2026-01-01T00:00:%02d.%03dZ" % (len(stored) // 1000, len(stored) % 1000),
                               "task_id": body.get("task_id")})
            if self.after_add is not None:
                result = self.after_add(self._scope(body), body)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, BaseException):
                    raise result
            return envelope({"accepted_ids": [row["id"] for row in stored[-len(messages):]],
                             "accepted_versions": ["v1"] * len(messages),
                             "total_count": len(stored)})
        if path == "/v3/conversation/query":
            rows = list(sessions.get(body.get("session_id"), []))
            offset = body.get("offset", 0)
            limit = body.get("limit", 20)
            page = rows[offset:offset + limit]
            scope = self._scope(body)
            messages = [{"id": row["id"], "session_id": body.get("session_id"),
                         "team_id": scope[0], "user_id": scope[2], "agent_id": scope[1],
                         "task_id": row.get("task_id"), "role": row["role"],
                         "content": row["content"], "timestamp": row["timestamp"]}
                        for row in page]
            return envelope({"messages": messages, "total": len(rows)})
        if path == "/v3/conversation/search":
            rows = [row for session in sessions.values() for row in session]
            tokens = [token for token in body.get("query", "").split() if token]
            hits = []
            for row in rows:
                matched = sum(row["content"].count(token) for token in tokens)
                if matched:
                    hits.append({"id": row["id"], "role": row["role"], "content": row["content"],
                                 "timestamp": row["timestamp"], "score": 0.1 * matched})
            hits.sort(key=lambda hit: hit["score"], reverse=True)
            return envelope({"messages": hits[:body.get("limit", 5)]})
        if path == "/v3/conversation/count":
            return envelope({"total": sum(len(session) for session in sessions.values())})
        raise AssertionError("Unexpected native API path: " + path)

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class SouthboundMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.backend = SouthboundMemory(client=self.fake)
        self.options = {"api_key": "test-only-placeholder", "service_id": "default",
                        "owner_id": "kpbot-user"}

    def record(self, *, kind="experience", project="project-a", content=None, **changes):
        data = {"kind": kind, "project_id": project, "owner_id": "kpbot-user",
                "task_id": "task-101", "visibility": "private",
                "content": {"conclusion": "原文\n\u0000😀", "evidence": ["不可改写\n", "proof"],
                            "nested": {"none": None, "float": 1.25, "bool": False,
                                       "int": 9007199254740993}} if content is None else content}
        data.update(changes)
        return data

    def request(self, *, projects=("project-a",), kinds=("experience", "task_process"), **changes):
        data = {"query": "循环 优化", "features": ["寄存器压力"],
                "sources": [{"project_id": project, "owner_id": "kpbot-user"} for project in projects],
                "kinds": list(kinds), "limit": 5}
        data.update(changes)
        return data

    async def init(self, **changes):
        result = await self.backend.initialize({**self.options, **changes})
        return result

    async def ready(self, **changes):
        result = await self.init(**changes)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["state"], "ready")
        return result

    def calls(self, path):
        return [call for call in self.fake.calls if call[1] == path]

    def error(self, result, code, *, source=False):
        self.assertEqual(set(result), {"ok", "error"})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result["error"]), {"code", "message", "source_id"})
        self.assertEqual(result["error"]["code"], code)
        if source:
            self.assertTrue(result["error"]["source_id"])
        return result

    # ── initialize / status ────────────────────────────────────────────────

    async def test_initialize_ready_with_search_notice(self):
        result = await self.ready()
        self.assertEqual(result["data"]["supports_shared"], False)
        self.assertIn("BM25 keyword search", result["data"]["message"])
        self.assertIn("not probed", result["data"]["message"])

    async def test_initialize_sends_bearer_and_service_id_headers(self):
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return FakeClient(**kwargs)

        backend = SouthboundMemory(client_factory=factory)
        result = await backend.initialize(dict(self.options), timeout=30)
        self.assertTrue(result["ok"], result)
        headers = captured["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-only-placeholder")
        self.assertEqual(headers["x-tdai-service-id"], "default")

    async def test_initialize_rejects_invalid_options(self):
        for options in ("nope", None, 5):
            result = await self.backend.initialize(options)
            self.error(result, "INVALID_ARGUMENT")
        for changes in (
                {"endpoint": "http://user:pass@127.0.0.1:8420"},
                {"endpoint": "http://127.0.0.1:8420/path"},
                {"endpoint": "http://127.0.0.1:8420/?q=1"},
                {"endpoint": "http://127.0.0.1:8420/#frag"},
                {"endpoint": "ftp://127.0.0.1:8420"},
                {"endpoint": ":///missing-host"},
                {"endpoint": "http://ha st:8420"},
                {"endpoint": 5},
                {"api_key": ""},
                {"api_key": "bad\r\nX-Injected: 1"},
                {"service_id": ""},
                {"service_id": "x" * 129},
                {"owner_id": " "},
                {"agent_id": "x\u0001y"},
                {"max_input_bytes": "big"},
                {"max_input_bytes": 0},
                {"max_input_bytes": 33_554_433},
                {"chunk_chars": 511},
                {"chunk_chars": 8001},
                {"unexpected": "option"},
        ):
            result = await self.init(**changes)
            self.error(result, "INVALID_ARGUMENT")

    async def test_initialize_uses_documented_defaults(self):
        result = await self.ready()
        self.assertEqual(self.backend._options["endpoint"], "http://127.0.0.1:8420")
        self.assertEqual(self.backend._options["api_key"], "test-only-placeholder")
        self.assertEqual(self.backend._options["agent_id"], "kpbot")
        self.assertEqual(self.backend._options["max_input_bytes"], 2_097_152)
        self.assertEqual(self.backend._options["chunk_chars"], 8000)

    async def test_reinitialize_same_config_keeps_data(self):
        await self.ready()
        stored = await self.backend.store_experience(self.record(content="循环 原文"), timeout=30)
        self.assertTrue(stored["ok"], stored)
        again = await self.ready()
        self.assertEqual(again["data"]["state"], "ready")
        found = await self.backend.search(self.request(), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(len(found["data"]["items"]), 1)

    async def test_reinitialize_different_config_is_rejected(self):
        await self.ready()
        result = await self.init(agent_id="other")
        self.error(result, "INVALID_ARGUMENT")

    async def test_initialize_after_close_is_not_ready(self):
        await self.ready()
        self.assertIsNone(await self.backend.close())
        result = await self.init()
        self.error(result, "NOT_READY")

    async def test_unhealthy_gateway_is_unavailable(self):
        for health in ({"status": "degraded"}, {"status": "ok", "stores": {"ftsAvailable": False,
                                                                           "embeddingService": False}}):
            self.fake.health = health
            result = await self.init()
            self.error(result, "UNAVAILABLE")
            status = await self.backend.status()
            self.assertEqual(status["data"]["state"], "unavailable")

    async def test_embedding_only_gateway_is_ready_with_vector_notice(self):
        self.fake.health = {"status": "ok", "stores": {"ftsAvailable": False,
                                                       "embeddingService": True}}
        result = await self.ready()
        self.assertIn("vector embedding search", result["data"]["message"])

    async def test_probe_denied_credentials_map_to_access_denied(self):
        self.fake.handlers["/v3/conversation/count"] = lambda method, kwargs: error_envelope(401)
        result = await self.init()
        self.error(result, "ACCESS_DENIED")

    async def test_probe_connection_failure_is_unavailable(self):
        self.fake.handlers["/health"] = lambda method, kwargs: ConnectionError("boom")
        result = await self.init()
        self.error(result, "UNAVAILABLE")

    async def test_status_before_initialize_reports_uninitialized(self):
        status = await self.backend.status()
        self.assertEqual(status["data"]["state"], "uninitialized")
        self.assertEqual(status["data"]["supports_shared"], False)

    async def test_status_reprobes_and_recovers(self):
        await self.ready()
        broken = {"status": "ok", "stores": {"ftsAvailable": False, "embeddingService": False}}
        self.fake.health = broken
        status = await self.backend.status()
        self.assertEqual(status["data"]["state"], "unavailable")
        self.fake.health = {"status": "ok", "stores": {"ftsAvailable": True,
                                                       "embeddingService": False}}
        status = await self.backend.status()
        self.assertEqual(status["data"]["state"], "ready")

    async def test_status_timeout_is_reported(self):
        await self.ready()
        self.fake.delays["/health"] = 0.5
        status = await self.backend.status(timeout=0.1)
        self.error(status, "TIMEOUT")

    # ── store_experience / store_process ──────────────────────────────────

    async def test_store_experience_roundtrip_small_record(self):
        await self.ready()
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["data"]), {"source_id", "processing"})
        self.assertRegex(result["data"]["source_id"], r"^kpbot-mem-[0-9a-f]{32}$")
        self.assertEqual(result["data"]["processing"], "unknown")

    async def test_store_process_roundtrip_with_tool_roles(self):
        await self.ready()
        content = [{"role": "user", "content": "示例任务：优化计算循环"},
                   {"role": "assistant", "content": "示例执行记录：检查重复搬移"},
                   {"role": "tool", "content": "基准测试完成"},
                   {"role": "assistant", "content": "最终结果"}]
        stored = await self.backend.store_process(self.record(kind="task_process", content=content), timeout=30)
        self.assertTrue(stored["ok"], stored)
        found = await self.backend.search(self.request(), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"]["items"][0]["content"], content)

    async def test_store_chunks_large_record_with_parseable_headers(self):
        await self.ready(chunk_chars=512)
        content = "循环 优化 " + "原文保持\n" * 400
        result = await self.backend.store_experience(self.record(content=content), timeout=30)
        self.assertTrue(result["ok"], result)
        source_id = result["data"]["source_id"]
        rows = self.fake.store[("project-a", "kpbot", "kpbot-user")][source_id]
        self.assertGreater(len(rows), 2)
        first = rows[0]["content"]
        self.assertRegex(first,
                         r"^\[\[kpbot-memory:1:kpbot-mem-[0-9a-f]{32}:experience:0:\d+\]\]\n")
        for row in rows:
            self.assertLessEqual(len(row["content"]), 8192)
            self.assertTrue(row["content"].startswith("[[kpbot-memory:1:" + source_id))
        found = await self.backend.search(self.request(), timeout=30)
        self.assertEqual(found["data"]["items"][0]["content"], content)

    async def test_store_batches_beyond_one_hundred_messages(self):
        await self.ready(chunk_chars=512)
        content = "原文 " * 40000
        result = await self.backend.store_experience(self.record(content=content), timeout=60)
        self.assertTrue(result["ok"], result)
        adds = self.calls("/v3/conversation/add")
        self.assertGreaterEqual(len(adds), 2)
        for _, _, kwargs in adds:
            self.assertLessEqual(len(kwargs["json"]["messages"]), 100)

    async def test_store_write_then_readback_mismatch_is_write_unknown(self):
        await self.ready()

        def tamper(scope, body):
            session = self.fake.store[scope][body["session_id"]]
            session[-1]["content"] = session[-1]["content"] + "tampered"

        self.fake.after_add = tamper
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.error(result, "WRITE_UNKNOWN", source=True)

    async def test_store_native_rejection_with_empty_session_reports_cause(self):
        await self.ready()
        for status, code in ((400, "INVALID_ARGUMENT"), (413, "TOO_LARGE"),
                             (403, "ACCESS_DENIED")):
            self.fake.handlers["/v3/conversation/add"] = (
                lambda method, kwargs, status=status: error_envelope(status))
            result = await self.backend.store_experience(self.record(), timeout=30)
            self.error(result, code, source=True)
            self.fake.handlers.pop("/v3/conversation/add", None)

    async def test_store_uncertain_rejection_with_empty_session_stays_unknown(self):
        await self.ready()
        for status, code in ((500, "BACKEND_ERROR"), (503, "UNAVAILABLE")):
            self.fake.handlers["/v3/conversation/add"] = (
                lambda method, kwargs, status=status: error_envelope(status))
            result = await self.backend.store_experience(self.record(), timeout=30)
            self.error(result, "WRITE_UNKNOWN", source=True)
            self.fake.handlers.pop("/v3/conversation/add", None)

    async def test_initialize_recovers_after_failure(self):
        broken = {"status": "ok", "stores": {"ftsAvailable": False, "embeddingService": False}}
        self.fake.health = broken
        result = await self.init()
        self.error(result, "UNAVAILABLE")
        status = await self.backend.status()
        self.assertEqual(status["data"]["state"], "unavailable")
        self.fake.health = {"status": "ok", "stores": {"ftsAvailable": True,
                                                       "embeddingService": False}}
        recovered = await self.ready()
        self.assertEqual(recovered["data"]["state"], "ready")

    async def test_store_native_rejection_after_partial_write_is_unknown(self):
        await self.ready(chunk_chars=512)
        content = "原文 " * 40000
        state = {"count": 0}

        def fail_second(method, kwargs):
            state["count"] += 1
            return error_envelope(400) if state["count"] >= 2 else None

        self.fake.handlers["/v3/conversation/add"] = fail_second
        result = await self.backend.store_experience(self.record(content=content), timeout=60)
        self.error(result, "WRITE_UNKNOWN", source=True)

    async def test_store_timeout_after_write_started_is_write_unknown(self):
        await self.ready()
        self.fake.delays["/v3/conversation/add"] = 0.5
        result = await self.backend.store_experience(self.record(), timeout=0.15)
        self.error(result, "WRITE_UNKNOWN", source=True)

    async def test_store_cancellation_propagates_with_source(self):
        await self.ready()
        self.fake.handlers["/v3/conversation/add"] = lambda method, kwargs: asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.backend.store_experience(self.record(), timeout=30)
        self.assertRegex(caught.exception.source_id, r"^kpbot-mem-[0-9a-f]{32}$")

    async def test_store_too_large_rejects_before_any_write(self):
        await self.ready(max_input_bytes=200)
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.error(result, "TOO_LARGE")
        self.assertIn("200", result["error"]["message"])
        self.assertEqual(self.calls("/v3/conversation/add"), [])

    async def test_store_at_exact_limit_succeeds(self):
        envelope_bytes = len(canonical(
            {"schema_version": 1, "source_id": "kpbot-mem-" + "0" * 32,
             "record": json.loads(canonical(self.record())),
             "record_sha256": "0" * 64}))
        await self.ready(max_input_bytes=envelope_bytes)
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.assertTrue(result["ok"], result)

    async def test_store_validates_record_contract(self):
        await self.ready()
        cases = [
            (self.record(project=" "), "INVALID_ARGUMENT"),
            (self.record(task_id=""), "INVALID_ARGUMENT"),
            (self.record(visibility="shared"), "UNSUPPORTED"),
            (self.record(owner_id="someone-else"), "ACCESS_DENIED"),
            (self.record(content=42), "INVALID_ARGUMENT"),
            ({"kind": "experience"}, "INVALID_ARGUMENT"),
            (self.record(kind="experience"), None),
        ]
        for record, code in cases:
            if code is None:
                continue
            result = await self.backend.store_experience(record, timeout=30)
            self.error(result, code)
        result = await self.backend.store_experience(self.record(kind="task_process"), timeout=30)
        self.error(result, "INVALID_ARGUMENT")

    async def test_store_non_json_content_is_rejected(self):
        await self.ready()
        record = self.record()
        record["content"] = {"bad": float("nan")}
        result = await self.backend.store_experience(record, timeout=30)
        self.error(result, "INVALID_ARGUMENT")
        record = self.record()
        record["content"] = {"tuple": (1, 2)}
        result = await self.backend.store_experience(record, timeout=30)
        self.assertIn(result["error"]["code"], ("INVALID_ARGUMENT", "WRITE_UNKNOWN"))
        self.assertEqual(self.calls("/v3/conversation/add"), [])

    async def test_store_requires_initialization(self):
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.error(result, "NOT_READY")
        await self.ready()
        self.assertIsNone(await self.backend.close())
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.error(result, "NOT_READY")

    async def test_store_invalid_timeout_is_rejected(self):
        await self.ready()
        for timeout in (0, -1, float("nan"), float("inf"), "30", None):
            result = await self.backend.store_experience(self.record(), timeout=timeout)
            self.error(result, "INVALID_ARGUMENT")

    # ── search ─────────────────────────────────────────────────────────────

    async def test_search_returns_originals_across_projects(self):
        await self.ready()
        first = await self.backend.store_experience(
            self.record(project="project-a", content={"text": "循环 优化 经验一"}), timeout=30)
        second = await self.backend.store_process(
            self.record(kind="task_process", project="project-b",
                        content=[{"role": "user", "content": "循环 优化 过程二"}]), timeout=30)
        self.assertTrue(first["ok"] and second["ok"], (first, second))
        found = await self.backend.search(self.request(projects=("project-a", "project-b")), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"]["partial"], False)
        by_source = {item["source_id"]: item for item in found["data"]["items"]}
        self.assertIn(first["data"]["source_id"], by_source)
        self.assertIn(second["data"]["source_id"], by_source)
        item = by_source[first["data"]["source_id"]]
        self.assertEqual(item["content"], {"text": "循环 优化 经验一"})
        self.assertEqual(item["content_type"], "original")
        self.assertEqual(item["kind"], "experience")
        self.assertEqual(item["project_id"], "project-a")
        self.assertEqual(item["owner_id"], "kpbot-user")
        self.assertEqual(item["task_id"], "task-101")
        self.assertIsInstance(item["score"], float)
        process = by_source[second["data"]["source_id"]]
        self.assertEqual(process["kind"], "task_process")
        self.assertEqual(process["project_id"], "project-b")

    async def test_search_orders_items_by_score(self):
        await self.ready()
        low = await self.backend.store_experience(
            self.record(content={"text": "循环 优化"}), timeout=30)
        high = await self.backend.store_experience(
            self.record(task_id="task-102", content={"text": "循环 优化 循环 优化 循环 优化"}), timeout=30)
        self.assertTrue(low["ok"] and high["ok"])
        found = await self.backend.search(self.request(limit=2), timeout=30)
        items = found["data"]["items"]
        self.assertEqual(len(items), 2)
        self.assertGreaterEqual(items[0]["score"], items[1]["score"])
        self.assertEqual(items[0]["source_id"], high["data"]["source_id"])

    async def test_search_filters_by_kind(self):
        await self.ready()
        experience = await self.backend.store_experience(
            self.record(content={"text": "循环 优化"}), timeout=30)
        process = await self.backend.store_process(
            self.record(kind="task_process", task_id="task-202",
                        content=[{"role": "user", "content": "循环 优化"}]), timeout=30)
        self.assertTrue(experience["ok"] and process["ok"])
        found = await self.backend.search(self.request(kinds=("experience",)), timeout=30)
        self.assertTrue(found["ok"], found)
        sources = [item["source_id"] for item in found["data"]["items"]]
        self.assertIn(experience["data"]["source_id"], sources)
        self.assertNotIn(process["data"]["source_id"], sources)

    async def test_search_without_hits_is_empty_not_partial(self):
        await self.ready()
        found = await self.backend.search(self.request(query="不存在的词"), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"]["items"], [])
        self.assertEqual(found["data"]["partial"], False)

    async def test_search_ignores_foreign_messages_in_scope(self):
        await self.ready()
        sessions = self.fake._rows({"team_id": "project-a", "agent_id": "kpbot", "user_id": "kpbot-user"})
        sessions["foreign-session"] = [{"id": "msg-x", "role": "user",
                                        "content": "循环 优化 普通消息",
                                        "timestamp": "2026-01-01T00:00:00Z", "task_id": None}]
        found = await self.backend.search(self.request(), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"]["items"], [])

    async def test_search_skips_corrupt_read_and_reports_partial(self):
        await self.ready()
        good = await self.backend.store_experience(
            self.record(content={"text": "循环 优化 完整"}), timeout=30)
        bad = await self.backend.store_experience(
            self.record(task_id="task-303", content={"text": "循环 优化 损坏"}), timeout=30)
        self.assertTrue(good["ok"] and bad["ok"], (good, bad))
        scope = ("project-a", "kpbot", "kpbot-user")
        session = self.fake.store[scope][bad["data"]["source_id"]]
        session[0]["content"] = session[0]["content"].replace("损坏", "篡改")
        found = await self.backend.search(self.request(limit=5), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["data"]["partial"], True)
        sources = [item["source_id"] for item in found["data"]["items"]]
        self.assertIn(good["data"]["source_id"], sources)
        self.assertNotIn(bad["data"]["source_id"], sources)

    async def test_search_all_reads_failed_is_read_failed(self):
        await self.ready()
        stored = await self.backend.store_experience(
            self.record(content={"text": "循环 优化"}), timeout=30)
        self.assertTrue(stored["ok"], stored)
        scope = ("project-a", "kpbot", "kpbot-user")
        session = self.fake.store[scope][stored["data"]["source_id"]]
        session[0]["content"] = session[0]["content"][:-4]
        result = await self.backend.search(self.request(), timeout=30)
        self.error(result, "READ_FAILED")

    async def test_search_source_failure_fails_whole_operation(self):
        await self.ready()
        def fail_search(method, kwargs):
            if kwargs["json"].get("team_id") == "project-b":
                return error_envelope(500)
            return None
        self.fake.handlers["/v3/conversation/search"] = fail_search
        result = await self.backend.search(
            self.request(projects=("project-a", "project-b")), timeout=30)
        self.error(result, "BACKEND_ERROR")

    async def test_search_validates_request_contract(self):
        await self.ready()
        for changes, code in (
                ({"include_shared": True}, "UNSUPPORTED"),
                ({"include_shared": "yes"}, "UNSUPPORTED"),
                ({"sources": [{"project_id": "project-a", "owner_id": "attacker"}]}, "ACCESS_DENIED"),
                ({"sources": []}, "INVALID_ARGUMENT"),
                ({"kinds": []}, "INVALID_ARGUMENT"),
                ({"kinds": ["nope"]}, "INVALID_ARGUMENT"),
                ({"limit": 0}, "INVALID_ARGUMENT"),
                ({"limit": 21}, "INVALID_ARGUMENT"),
                ({"limit": "5"}, "INVALID_ARGUMENT"),
                ({"limit": True}, "INVALID_ARGUMENT"),
                ({"query": "", "features": []}, "INVALID_ARGUMENT"),
                ({"query": "  ", "features": ["  "]}, "INVALID_ARGUMENT"),
                ({"features": "循环"}, "INVALID_ARGUMENT"),
        ):
            result = await self.backend.search(self.request(**changes), timeout=30)
            self.error(result, code)
        missing = self.request()
        del missing["query"]
        result = await self.backend.search(missing, timeout=30)
        self.error(result, "INVALID_ARGUMENT")
        result = await self.backend.search("not a dict", timeout=30)
        self.error(result, "INVALID_ARGUMENT")

    async def test_search_truncates_native_query_to_backend_limit(self):
        await self.ready()
        found = await self.backend.search(self.request(query="循环 " + "x" * 3000), timeout=30)
        self.assertTrue(found["ok"], found)
        searches = self.calls("/v3/conversation/search")
        self.assertTrue(searches)
        self.assertLessEqual(len(searches[-1][2]["json"]["query"]), 2048)

    async def test_search_features_are_appended_without_intent_generation(self):
        await self.ready()
        found = await self.backend.search(self.request(query="循环"), timeout=30)
        self.assertTrue(found["ok"], found)
        query = self.calls("/v3/conversation/search")[-1][2]["json"]["query"]
        self.assertEqual(query, "循环\nFeatures:\n寄存器压力")

    async def test_search_deduplicates_chunks_of_one_session(self):
        await self.ready(chunk_chars=512)
        content = "循环 优化 " + "原文保持\n" * 300
        stored = await self.backend.store_experience(self.record(content=content), timeout=30)
        self.assertTrue(stored["ok"], stored)
        scope = ("project-a", "kpbot", "kpbot-user")
        self.assertGreater(len(self.fake.store[scope][stored["data"]["source_id"]]), 2)
        found = await self.backend.search(self.request(query="循环", limit=20), timeout=30)
        self.assertTrue(found["ok"], found)
        matches = [item for item in found["data"]["items"]
                   if item["source_id"] == stored["data"]["source_id"]]
        self.assertEqual(len(matches), 1)

    async def test_search_requires_initialization(self):
        result = await self.backend.search(self.request(), timeout=30)
        self.error(result, "NOT_READY")

    async def test_search_timeout_is_reported(self):
        await self.ready()
        self.fake.delays["/v3/conversation/search"] = 0.5
        result = await self.backend.search(self.request(), timeout=0.1)
        self.error(result, "TIMEOUT")

    # ── close / concurrency ────────────────────────────────────────────────

    async def test_close_is_idempotent_and_reports_closed(self):
        await self.ready()
        self.assertIsNone(await self.backend.close())
        self.assertIsNone(await self.backend.close())
        status = await self.backend.status()
        self.assertEqual(status["data"]["state"], "closed")
        self.assertTrue(self.fake.closed)

    async def test_data_survives_close_and_reinstantiation(self):
        await self.ready()
        stored = await self.backend.store_experience(self.record(content={"text": "循环 优化"}), timeout=30)
        self.assertTrue(stored["ok"], stored)
        self.assertIsNone(await self.backend.close())
        fresh = SouthboundMemory(client=self.fake)
        result = await fresh.initialize(dict(self.options), timeout=30)
        self.assertTrue(result["ok"], result)
        found = await fresh.search(self.request(), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertIn(stored["data"]["source_id"],
                      [item["source_id"] for item in found["data"]["items"]])

    async def test_close_waits_for_inflight_data_calls(self):
        await self.ready()
        self.fake.delays["/v3/conversation/search"] = 0.3
        task = asyncio.create_task(self.backend.search(self.request(), timeout=30))
        await asyncio.sleep(0.05)
        closing = asyncio.create_task(self.backend.close())
        done, _ = await asyncio.wait({task, closing}, timeout=0.2)
        self.assertNotIn(closing, done)
        await asyncio.wait_for(asyncio.gather(task, closing), timeout=5)
        self.assertTrue(task.result()["ok"])
        rejected = await self.backend.search(self.request(), timeout=30)
        self.error(rejected, "NOT_READY")

    async def test_concurrent_stores_and_search(self):
        await self.ready()
        records = [self.record(task_id=f"task-{index}", content={"text": f"循环 优化 {index}"})
                   for index in range(3)]
        results = await asyncio.gather(
            self.backend.store_experience(records[0], timeout=30),
            self.backend.store_experience(records[1], timeout=30),
            self.backend.search(self.request(), timeout=30))
        self.assertTrue(all(result["ok"] for result in results), results)
        found = await self.backend.search(self.request(limit=20), timeout=30)
        self.assertTrue(found["ok"], found)
        self.assertEqual(len(found["data"]["items"]), 2)

    async def test_close_failure_returns_backend_error(self):
        await self.ready()

        async def fail():
            raise RuntimeError("boom")

        self.fake.aclose = fail
        result = await self.backend.close()
        self.error(result, "BACKEND_ERROR")


if __name__ == "__main__":
    unittest.main()
