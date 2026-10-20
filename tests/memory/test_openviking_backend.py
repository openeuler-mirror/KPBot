"""OpenViking adapter tests: stdlib unittest, fake HTTP only, no service/model.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_openviking_backend -v
"""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kpbot_memory.backends import OpenVikingBackend


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class FakeResponse:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code = status
        self.data = data
        self.content = content

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return copy.deepcopy(self.data)


def ok(result):
    return FakeResponse(data={"status": "ok", "result": result})


def native_error(code="INTERNAL", status=500):
    return FakeResponse(status, {"status": "error", "error": {
        "code": code, "message": "DO NOT EXPOSE: credential and complete fixture body"}})


class FakeClient:
    """Implements only httpx request/aclose; resources survive client replacement."""
    def __init__(self, resources=None):
        self.resources = resources if resources is not None else {}
        self.calls = []
        self.closed = False
        self.close_count = 0
        self.delays = {}
        self.handlers = {}
        self.after_write = None
        self.write_status = {"semantic_status": "skipped", "vector_status": "complete"}
        self.extra_hits = []
        self.scores = {}
        self.health = {"status": "ok", "healthy": True, "auth_mode": "api_key",
                       "account_id": "account-1", "user_id": "alice", "role": "user"}
        self.ready = {"status": "ready", "checks": {
            "agfs": {"status": "ok", "checks": {"filesystem": "ok"}},
            "vectordb": "ok", "embedding": "ok"}}
        self.user = "alice"

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
            return result
        if path == "/health":
            return FakeResponse(data=self.health)
        if path == "/ready":
            return FakeResponse(data=self.ready)
        if path == "/api/v1/system/status":
            return ok({"initialized": True, "user": self.user})
        if path == "/api/v1/fs/ls":
            return ok([])
        if path == "/api/v1/content/write":
            body = kwargs["json"]
            uri = body["uri"]
            if uri in self.resources:
                return native_error("ALREADY_EXISTS", 409)
            self.resources[uri] = body["content"].encode("utf-8")
            if self.after_write is not None:
                result = self.after_write(uri, kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, BaseException):
                    raise result
                return result
            return ok({"uri": uri, "written_bytes": len(self.resources[uri]),
                       **self.write_status})
        if path == "/api/v1/content/download":
            uri = kwargs["params"]["uri"]
            if uri not in self.resources:
                return native_error("NOT_FOUND", 404)
            return FakeResponse(content=self.resources[uri])
        if path == "/api/v1/search/find":
            targets = kwargs["json"]["target_uri"]
            hits = []
            for uri in self.resources:
                if uri.rpartition("/")[0] in targets:
                    hits.append({"uri": uri, "level": 2, "context_type": "resource",
                                 "score": self.scores.get(uri, 0.72), "abstract": "not original"})
            hits.extend(copy.deepcopy(self.extra_hits))
            hits.sort(key=lambda hit: hit.get("score") if isinstance(hit.get("score"), (int, float))
                      else -1, reverse=True)
            return ok({"resources": hits[:kwargs["json"]["limit"]],
                       "memories": [], "skills": [], "total": len(hits)})
        raise AssertionError("Unexpected native API path")

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class OpenVikingBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.backend = OpenVikingBackend(client=self.fake)
        self.options = {"api_key": "test-only-placeholder", "account_id": "account-1",
                        "user_id": "alice", "owner_id": "kpbot-user"}

    def record(self, *, kind="experience", project="project-a", content=None, **changes):
        data = {"kind": kind, "project_id": project, "owner_id": "kpbot-user",
                "task_id": "task-101", "visibility": "private",
                "content": {"conclusion": "原文\n\u0000😀", "evidence": ["不可改写\n", "proof"],
                            "nested": {"none": None, "float": 1.25, "bool": False,
                                       "int": 9007199254740993}} if content is None else content}
        data.update(changes)
        return data

    def request(self, *, projects=("project-a",), kinds=("experience", "task_process"), **changes):
        data = {"query": "问题原文", "features": ["寄存器压力", "循环优化"],
                "sources": [{"project_id": project, "owner_id": "kpbot-user"} for project in projects],
                "kinds": list(kinds), "limit": 5}
        data.update(changes)
        return data

    async def init(self, **changes):
        result = await self.backend.initialize({**self.options, **changes})
        self.assertTrue(result["ok"], result)
        return result

    def assert_error(self, result, code, source=None):
        self.assertEqual(set(result), {"ok", "error"})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result["error"]), {"code", "message", "source_id"})
        self.assertEqual(result["error"]["code"], code, result)
        self.assertEqual(result["error"]["source_id"], source)
        self.assertNotIn("DO NOT EXPOSE", result["error"]["message"])

    async def store(self, record=None):
        record = self.record() if record is None else record
        method = self.backend.store_experience if record["kind"] == "experience" else self.backend.store_process
        result = await method(record)
        self.assertTrue(result["ok"], result)
        return result["data"]["source_id"]

    async def test_lifecycle_and_no_dependency_at_construction(self):
        with mock.patch.dict(sys.modules, {"httpx": None}):
            backend = OpenVikingBackend()
            before = await backend.status()
            self.assertEqual(before["data"]["state"], "uninitialized")
            self.assert_error(await backend.store_experience(self.record()), "NOT_READY")
            self.assert_error(await backend.initialize(self.options), "UNAVAILABLE")
            self.assertEqual((await backend.status())["data"]["state"], "unavailable")
            await backend.close()
        result = await self.init()
        self.assertEqual(set(result), {"ok", "data"})
        self.assertEqual(result["data"]["state"], "ready")
        self.assertFalse(result["data"]["supports_shared"])
        self.assertIn("inference", result["data"]["message"])
        self.assertIsNone(await self.backend.close())
        self.assertIsNone(await self.backend.close())
        self.assertEqual(self.fake.close_count, 1)
        self.assertEqual((await self.backend.status())["data"]["state"], "closed")
        self.assert_error(await self.backend.initialize(self.options), "NOT_READY")
        self.assert_error(await self.backend.search(self.request()), "NOT_READY")

    async def test_factory_uses_explicit_key_without_identity_headers_proxy_or_redirect(self):
        kwargs_seen = {}
        def factory(**kwargs):
            kwargs_seen.update(kwargs)
            return self.fake
        backend = OpenVikingBackend(client_factory=factory)
        self.assertTrue((await backend.initialize(self.options))["ok"])
        self.assertEqual(kwargs_seen["base_url"], "http://127.0.0.1:1933")
        self.assertEqual(kwargs_seen["headers"], {"X-API-Key": "test-only-placeholder"})
        self.assertFalse(kwargs_seen["follow_redirects"])
        self.assertFalse(kwargs_seen["trust_env"])
        self.assertEqual([path for _, path, _ in self.fake.calls], [
            "/health", "/api/v1/system/status", "/ready", "/api/v1/fs/ls"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.fake.calls))
        self.assertTrue(all(kwargs["timeout"] > 0 for _, _, kwargs in self.fake.calls))
        await backend.close()

    async def test_initialize_same_config_preserves_data_changed_config_rejected(self):
        await self.init()
        uri = await self.store()
        self.assertTrue((await self.backend.initialize(self.options))["ok"])
        self.assertIn(uri, self.fake.resources)
        self.assert_error(await self.backend.initialize({**self.options, "owner_id": "other"}), "INVALID_ARGUMENT")
        self.assertEqual((await self.backend.status())["data"]["state"], "ready")

    async def test_invalid_options_and_loopback_enforcement(self):
        for changes in ({"endpoint": "https://example.com"}, {"endpoint": "http://user:pass@localhost:1933"},
                        {"endpoint": "http://127.0.0.1:1933?key=secret"}, {"endpoint": "http://localhost/base"},
                        {"endpoint": "http://["}, {"endpoint": "http://localhost:bad"},
                        {"endpoint": 3}, {"api_key": "\nnot-a-key"}, {"user_id": "../alice"},
                        {"processing_mode": "none"}, {"max_input_bytes": True}, {"unknown": 1}):
            with self.subTest(changes=changes):
                self.assert_error(await self.backend.initialize({**self.options, **changes}), "INVALID_ARGUMENT")
        self.assert_error(await self.backend.initialize({}), "INVALID_ARGUMENT")
        self.assertEqual(self.fake.calls, [])
        await self.init(endpoint="http://[::1]:1933/")

    async def test_ordinary_identity_must_match_both_authenticated_probes(self):
        original = copy.deepcopy(self.fake.health)
        for field, value in (("account_id", "other"), ("user_id", "other"),
                             ("role", "root"), ("role", "admin"), ("auth_mode", "dev")):
            with self.subTest(field=field, value=value):
                self.fake.health = {**original, field: value}
                self.assert_error(await self.backend.initialize(self.options), "ACCESS_DENIED")
        self.fake.health = original
        self.fake.user = "someone-else"
        self.assert_error(await self.backend.initialize(self.options), "ACCESS_DENIED")
        self.fake.user = "alice"
        await self.init()  # Failed initialization remains retryable.

    async def test_health_ok_does_not_bypass_invalid_key(self):
        self.fake.handlers["/api/v1/system/status"] = lambda *_: native_error("UNAUTHENTICATED", 401)
        self.assert_error(await self.backend.initialize(self.options), "ACCESS_DENIED")
        self.assertFalse(self.fake.resources)
        self.assert_error(await self.backend.store_experience(self.record()), "NOT_READY")

    async def test_ready_requires_vector_storage_and_dependency_checks(self):
        for field, value in (("embedding", "not_initialized"), ("vectordb", "not_configured")):
            with self.subTest(field=field):
                self.fake.ready["checks"][field] = value
                self.assert_error(await self.backend.initialize(self.options), "UNAVAILABLE")
                self.fake.ready["checks"][field] = "ok"
        await self.init()
        self.fake.handlers["/api/v1/fs/ls"] = lambda *_: native_error("PERMISSION_DENIED", 403)
        state = await self.backend.status()
        self.assertTrue(state["ok"])
        self.assertEqual(state["data"]["state"], "unavailable")
        self.fake.handlers.clear()
        self.assertEqual((await self.backend.status())["data"]["state"], "ready")

    async def test_full_roundtrip_two_kinds_and_native_resource_api(self):
        await self.init()
        experience = self.record()
        process = self.record(kind="task_process", content=[
            {"role": "user", "content": "输入"}, {"role": "tool", "content": {"raw": [1, 2]}},
            {"role": "assistant", "content": "完整最终结果\n\n"}])
        first = await self.store(experience)
        second = await self.store(process)
        self.assertNotEqual(first, second)
        self.assertIn("viking://user/alice/resources/kpbot/", first)
        self.assertNotIn("project-a", first)
        self.assertTrue(first.endswith(".json"))
        for uri, original in ((first, experience), (second, process)):
            envelope = json.loads(self.fake.resources[uri])
            self.assertEqual(envelope["record"], original)
            self.assertEqual(envelope["source_id"], uri)
        result = await self.backend.search(self.request())
        self.assertTrue(result["ok"])
        by_kind = {item["kind"]: item for item in result["data"]["items"]}
        self.assertEqual(by_kind["experience"]["content"], experience["content"])
        self.assertEqual(by_kind["task_process"]["content"], process["content"])
        self.assertTrue(all(item["content_type"] == "original" for item in by_kind.values()))
        self.assertFalse(result["data"]["partial"])
        writes = [kwargs["json"] for _, path, kwargs in self.fake.calls if path.endswith("/write")]
        self.assertTrue(all(body["mode"] == "create" and body["wait"] is True for body in writes))
        self.assertTrue(all(body["processing_mode"] == "vectors_only" for body in writes))
        paths = {path for _, path, _ in self.fake.calls}
        self.assertNotIn("/api/v1/sessions", paths)
        self.assertNotIn("/api/v1/resources", paths)
        self.assertNotIn("/api/v1/content/read", paths)

    async def test_plain_text_preserves_whitespace_unicode_and_newlines(self):
        await self.init()
        for text in ("", "\n", " \r\n尾随😀\n\n ", "\u0000raw\u2028text"):
            with self.subTest(text=text):
                uri = await self.store(self.record(content=text))
                result = await self.backend.search(self.request())
                item = next(item for item in result["data"]["items"] if item["source_id"] == uri)
                self.assertEqual(item["content"], text)

    async def test_multisource_kind_filter_and_native_ranking(self):
        await self.init()
        exp_a = await self.store(self.record(project="project-a"))
        proc_a = await self.store(self.record(project="project-a", kind="task_process"))
        exp_b = await self.store(self.record(project="project-b"))
        exp_c = await self.store(self.record(project="unrequested"))
        self.fake.scores.update({exp_a: 0.2, proc_a: 0.99, exp_b: 0.9, exp_c: 1.0})
        result = await self.backend.search(self.request(projects=("project-a", "project-b"),
                                                         kinds=("experience",), limit=1))
        self.assertTrue(result["ok"], result)
        self.assertEqual([item["source_id"] for item in result["data"]["items"]], [exp_b])
        finds = [kwargs["json"] for _, path, kwargs in self.fake.calls if path.endswith("/find")]
        self.assertEqual(len(finds), 2)
        for body in finds:
            self.assertEqual(body["context_type"], "resource")
            self.assertEqual(body["level"], 2)
            self.assertFalse(body["read_content"])
            self.assertTrue(all(target.endswith("/experience") for target in body["target_uri"]))
            self.assertEqual(body["query"], "问题原文\nFeatures:\n寄存器压力\n循环优化")
        self.assertEqual(result["data"]["items"][0]["score"], 0.9)

    async def test_any_source_failure_is_not_no_match_or_partial(self):
        await self.init()
        await self.store()
        original = self.fake.request
        calls = [0]
        async def fail_second(method, path, **kwargs):
            if path == "/api/v1/search/find":
                calls[0] += 1
                if calls[0] == 2:
                    return native_error("UNAVAILABLE", 503)
            return await original(method, path, **kwargs)
        self.fake.request = fail_second
        self.assert_error(await self.backend.search(self.request(projects=("project-a", "project-b"))), "UNAVAILABLE")
        self.assertEqual(calls[0], 2)

    async def test_no_hits_and_features_only_query(self):
        await self.init()
        result = await self.backend.search(self.request(query="", features=["feature"] ))
        self.assertEqual(result, {"ok": True, "data": {"items": [], "partial": False}})
        body = next(kwargs["json"] for _, path, kwargs in self.fake.calls if path.endswith("/find"))
        self.assertEqual(body["query"], "\nFeatures:\nfeature")

    async def test_scope_owner_visibility_and_request_validation(self):
        await self.init()
        self.assert_error(await self.backend.store_experience(self.record(owner_id="other")), "ACCESS_DENIED")
        self.assert_error(await self.backend.store_experience(self.record(visibility="shared")), "UNSUPPORTED")
        self.assert_error(await self.backend.search(self.request(include_shared=True)), "UNSUPPORTED")
        self.assert_error(await self.backend.search(self.request(sources=[{"owner_id": "other", "project_id": "project-a"}])), "ACCESS_DENIED")
        invalid = [{"limit": True}, {"limit": 21}, {"limit": 0}, {"sources": []}, {"kinds": []},
                   {"kinds": ["memory"]}, {"features": [1]}, {"query": 3},
                   {"query": " ", "features": ["\n"]}, {"sources": [{"project_id": "project-a"}]}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assert_error(await self.backend.search(self.request(**changes)), "INVALID_ARGUMENT")
        self.assert_error(await self.backend.search({}), "INVALID_ARGUMENT")
        self.assertFalse(self.fake.resources)

    async def test_non_json_content_missing_provenance_and_wrong_method(self):
        await self.init()
        for content in (42, True, None, (1, 2), {"x": float("nan")}, {1: "key"}, [object()]):
            record = self.record()
            record["content"] = content
            with self.subTest(type=type(content)):
                self.assert_error(await self.backend.store_experience(record), "INVALID_ARGUMENT")
        for field in ("kind", "project_id", "owner_id", "task_id", "visibility", "content"):
            record = self.record()
            del record[field]
            self.assert_error(await self.backend.store_experience(record), "INVALID_ARGUMENT")
        self.assert_error(await self.backend.store_process(self.record()), "INVALID_ARGUMENT")
        self.assertFalse(self.fake.resources)

    async def test_size_limit_is_bytes_of_complete_envelope_not_silent_truncation(self):
        await self.init(max_input_bytes=500)
        result = await self.backend.store_experience(self.record(content="😀" * 400))
        self.assert_error(result, "TOO_LARGE")
        self.assertIn("500 UTF-8 bytes", result["error"]["message"])
        self.assertFalse(self.fake.resources)
        self.assertFalse(any(path.endswith("/write") for _, path, _ in self.fake.calls))

    async def test_every_submission_is_new_uuid_no_replace_of_reviewed_input(self):
        await self.init()
        first = await self.store()
        second = await self.store()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.fake.resources), 2)
        self.assertEqual(json.loads(self.fake.resources[first])["record"], self.record())

    async def test_concurrent_calls_preserve_all_originals(self):
        await self.init()
        uris = await asyncio.gather(*(self.store(self.record(content={"index": index})) for index in range(12)))
        self.assertEqual(len(set(uris)), 12)
        self.assertEqual(len(self.fake.resources), 12)
        for index, uri in enumerate(uris):
            self.assertEqual(json.loads(self.fake.resources[uri])["record"]["content"], {"index": index})

    async def test_persistence_restart_uses_provider_resources_only(self):
        await self.init()
        uri = await self.store()
        await self.backend.close()
        restored_client = FakeClient(self.fake.resources)
        restored = OpenVikingBackend(client=restored_client)
        self.assertTrue((await restored.initialize(self.options))["ok"])
        result = await restored.search(self.request())
        self.assertEqual(result["data"]["items"][0]["source_id"], uri)
        self.assertEqual(result["data"]["items"][0]["content"], self.record()["content"])
        await restored.close()

    async def test_processing_uses_only_enabled_native_statuses(self):
        await self.init()
        for vector, expected in (("complete", "ready"), ("queued", "pending"), ("deferred", "pending"),
                                 ("failed", "failed"), ("skipped", "unknown"), (None, "unknown")):
            self.fake.write_status = {"semantic_status": "skipped", "vector_status": vector}
            result = await self.backend.store_experience(self.record())
            self.assertEqual(result["data"]["processing"], expected)
        await self.backend.close()
        self.backend = OpenVikingBackend(client=FakeClient(self.fake.resources))
        self.fake = self.backend._provided_client
        await self.init(processing_mode="semantic_and_vectors")
        self.assertIn("VLM", (await self.backend.status())["data"]["message"])
        self.fake.write_status = {"semantic_status": "deferred", "vector_status": "complete"}
        self.assertEqual((await self.backend.store_process(self.record(kind="task_process")))["data"]["processing"], "pending")
        self.fake.write_status = {"semantic_status": "failed", "vector_status": "complete"}
        self.assertEqual((await self.backend.store_experience(self.record()))["data"]["processing"], "failed")

    async def test_index_failure_after_confirmed_raw_storage_is_failed_not_lost(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("EMBEDDING_FAILED", 500)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "failed")
        self.assertIn(result["data"]["source_id"], self.fake.resources)

    async def test_connection_loss_but_verified_original_is_unknown_processing_no_retry(self):
        await self.init()
        self.fake.after_write = lambda *_: ConnectionError("DO NOT EXPOSE")
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_failed_confirmation_and_corrupted_write_are_write_unknown(self):
        await self.init()
        self.fake.handlers["/api/v1/content/download"] = lambda *_: native_error("NOT_FOUND", 404)
        result = await self.backend.store_experience(self.record())
        uri = next(iter(self.fake.resources))
        self.assert_error(result, "WRITE_UNKNOWN", uri)
        self.fake.handlers["/api/v1/content/download"] = lambda *_: FakeResponse(content=b"corrupted")
        result = await self.backend.store_experience(self.record())
        self.assert_error(result, "WRITE_UNKNOWN", list(self.fake.resources)[-1])
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 2)

    async def test_backend_write_rollback_is_not_success(self):
        await self.init()
        def rollback(uri, _):
            del self.fake.resources[uri]
            return native_error("EMBEDDING_FAILED", 500)
        self.fake.after_write = rollback
        result = await self.backend.store_experience(self.record())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "WRITE_UNKNOWN")
        self.assertIsInstance(result["error"]["source_id"], str)
        self.assertFalse(self.fake.resources)

    async def test_native_permission_error_without_original_is_conservatively_unknown(self):
        await self.init()
        self.fake.handlers["/api/v1/content/write"] = lambda *_: native_error("PERMISSION_DENIED", 403)
        result = await self.backend.store_experience(self.record())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "WRITE_UNKNOWN")
        self.assertIsInstance(result["error"]["source_id"], str)
        self.assertFalse(self.fake.resources)
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)
        self.assertEqual(sum(path.endswith("/download") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_400_retains_verified_original_with_unknown_processing(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("INVALID_ARGUMENT", 400)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertEqual(json.loads(self.fake.resources[result["data"]["source_id"]])["record"], self.record())
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)
        self.assertEqual(sum(path.endswith("/download") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_413_retains_original_instead_of_claiming_too_large(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("RESOURCE_EXHAUSTED", 413)
        result = await self.backend.store_process(self.record(kind="task_process"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertIn(result["data"]["source_id"], self.fake.resources)
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_403_is_verified_not_assumed_to_be_before_execution(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("PERMISSION_DENIED", 403)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertIn(result["data"]["source_id"], self.fake.resources)
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_403_and_inaccessible_original_is_write_unknown(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("PERMISSION_DENIED", 403)
        self.fake.handlers["/api/v1/content/download"] = lambda *_: native_error("PERMISSION_DENIED", 403)
        result = await self.backend.store_experience(self.record())
        self.assert_error(result, "WRITE_UNKNOWN", next(iter(self.fake.resources)))
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_unwritten_request_and_read_permission_denial_is_write_unknown(self):
        await self.init()
        self.fake.handlers["/api/v1/content/write"] = lambda *_: native_error("PERMISSION_DENIED", 403)
        self.fake.handlers["/api/v1/content/download"] = lambda *_: native_error("PERMISSION_DENIED", 403)
        result = await self.backend.store_experience(self.record())
        self.assertFalse(self.fake.resources)
        self.assertEqual(result["error"]["code"], "WRITE_UNKNOWN")
        self.assertIsInstance(result["error"]["source_id"], str)
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)
        self.assertEqual(sum(path.endswith("/download") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_unsupported_code_is_confirmed_not_dropped(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("UNIMPLEMENTED", 501)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_postwrite_processing_failure_mapped_to_400_still_returns_failed(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("PROCESSING_ERROR", 400)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "failed")
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_native_timeout_uses_remaining_budget_to_confirm_without_retry(self):
        await self.init()
        self.fake.after_write = lambda *_: native_error("DEADLINE_EXCEEDED", 504)
        result = await self.backend.store_experience(self.record())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["processing"], "unknown")
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)
        self.assertEqual(sum(path.endswith("/download") for _, path, _ in self.fake.calls), 1)

    async def test_partial_read_failure_and_all_failed_reads(self):
        await self.init()
        first, second = await self.store(), await self.store(self.record(kind="task_process"))
        original = self.fake.resources[first]
        self.fake.resources[first] = b"invalid JSON"
        result = await self.backend.search(self.request())
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["data"]["partial"])
        self.assertEqual([item["source_id"] for item in result["data"]["items"]], [second])
        self.fake.resources[second] = b"invalid JSON"
        self.assert_error(await self.backend.search(self.request()), "READ_FAILED")
        self.fake.resources[first] = original

    async def test_record_hash_and_locator_integrity_are_verified(self):
        await self.init()
        uri = await self.store()
        original = json.loads(self.fake.resources[uri])
        for change in ({"record_sha256": "bad"}, {"source_id": "viking://other"}, {"schema_version": True}):
            self.fake.resources[uri] = canonical({**original, **change})
            self.assert_error(await self.backend.search(self.request()), "READ_FAILED")
        self.fake.resources[uri] = b'{"schema_version":1,"schema_version":1}'
        self.assert_error(await self.backend.search(self.request()), "READ_FAILED")

    async def test_mismatched_envelope_scope_is_never_returned(self):
        await self.init()
        uri = await self.store()
        original = json.loads(self.fake.resources[uri])
        for change in ({"owner_id": "other"}, {"project_id": "project-other"},
                       {"kind": "task_process"}, {"visibility": "shared"}):
            envelope = copy.deepcopy(original)
            envelope["record"].update(change)
            envelope["record_sha256"] = hashlib.sha256(canonical(envelope["record"])).hexdigest()
            self.fake.resources[uri] = canonical(envelope)
            result = await self.backend.search(self.request(kinds=("experience",)))
            self.assertEqual(result, {"ok": True, "data": {"items": [], "partial": False}})

    async def test_unsafe_out_of_scope_nonresource_and_sidecar_hits_are_not_downloaded(self):
        await self.init()
        uri = await self.store()
        directory = uri.rpartition("/")[0]
        bad_uris = ["https://example.invalid/secret", "viking://resources/shared/x.json",
                    directory + "/../experience/" + "a" * 32 + ".json",
                    directory + "/%2e%2e/x.json", directory + "/.abstract.md",
                    directory + "/" + "a" * 32 + ".json?foo=x"]
        self.fake.extra_hits = [{"uri": value, "context_type": "resource", "level": 2, "score": 2}
                                for value in bad_uris]
        self.fake.extra_hits += [{"uri": uri, "context_type": "memory", "level": 2, "score": 2},
                                 {"uri": uri, "context_type": "resource", "level": True, "score": 2}]
        before = len(self.fake.calls)
        result = await self.backend.search(self.request(limit=20))
        self.assertEqual([item["source_id"] for item in result["data"]["items"]], [uri])
        downloaded = [kwargs["params"]["uri"] for _, path, kwargs in self.fake.calls[before:]
                      if path.endswith("/download")]
        self.assertEqual(downloaded, [uri])

    async def test_invalid_find_response_is_error_not_empty(self):
        await self.init()
        self.fake.handlers["/api/v1/search/find"] = lambda *_: ok({"resources": None})
        self.assert_error(await self.backend.search(self.request()), "BACKEND_ERROR")
        self.fake.handlers["/api/v1/search/find"] = lambda *_: FakeResponse(data=ValueError("DO NOT EXPOSE"))
        self.assert_error(await self.backend.search(self.request()), "BACKEND_ERROR")

    async def test_missing_score_maps_to_null_without_fabricating_probability(self):
        await self.init()
        uri = await self.store()
        self.fake.scores[uri] = None
        result = await self.backend.search(self.request())
        self.assertIsNone(result["data"]["items"][0]["score"])

    async def test_timeout_validation_is_strict_for_every_nonclose_method(self):
        for timeout in (0, -1, True, float("nan"), float("inf"), "30"):
            with self.subTest(timeout=timeout):
                self.assert_error(await self.backend.initialize(self.options, timeout=timeout), "INVALID_ARGUMENT")
                self.assert_error(await self.backend.status(timeout=timeout), "INVALID_ARGUMENT")
                self.assert_error(await self.backend.store_experience(self.record(), timeout=timeout), "INVALID_ARGUMENT")
                self.assert_error(await self.backend.store_process(self.record(kind="task_process"), timeout=timeout), "INVALID_ARGUMENT")
                self.assert_error(await self.backend.search(self.request(), timeout=timeout), "INVALID_ARGUMENT")
        self.assertFalse(self.fake.calls)

    async def test_total_deadline_includes_multiple_initialize_operations(self):
        self.fake.delays.update({"/health": 0.02, "/api/v1/system/status": 0.02})
        self.assert_error(await self.backend.initialize(self.options, timeout=0.03), "TIMEOUT")
        self.assertFalse(self.fake.resources)
        self.fake.delays.clear()
        await self.init()

    async def test_readonly_native_timeout_is_timeout_not_empty(self):
        await self.init()
        self.fake.handlers["/api/v1/search/find"] = lambda *_: TimeoutError("DO NOT EXPOSE")
        self.assert_error(await self.backend.search(self.request()), "TIMEOUT")
        self.fake.handlers["/api/v1/search/find"] = lambda *_: native_error("DEADLINE_EXCEEDED", 504)
        self.assert_error(await self.backend.search(self.request()), "TIMEOUT")

    async def test_timeout_after_write_started_returns_locator_and_no_retry(self):
        await self.init()
        async def hang(uri, _):
            await asyncio.sleep(1)
        self.fake.after_write = hang
        result = await self.backend.store_experience(self.record(), timeout=0.02)
        uri = next(iter(self.fake.resources))
        self.assert_error(result, "WRITE_UNKNOWN", uri)
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)
        self.assertFalse(any(path.endswith("/download") for _, path, _ in self.fake.calls))

    async def test_timeout_of_confirmation_is_write_unknown(self):
        await self.init()
        self.fake.delays["/api/v1/content/download"] = 1
        result = await self.backend.store_process(self.record(kind="task_process"), timeout=0.02)
        self.assert_error(result, "WRITE_UNKNOWN", next(iter(self.fake.resources)))

    async def test_cancellation_propagates_with_saved_resource_and_no_retry(self):
        await self.init()
        started = asyncio.Event()
        async def block(uri, _):
            started.set()
            await asyncio.Event().wait()
        self.fake.after_write = block
        task = asyncio.create_task(self.backend.store_experience(self.record()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(len(self.fake.resources), 1)
        self.assertEqual(caught.exception.source_id, next(iter(self.fake.resources)))
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_outer_deadline_preserves_locator_on_cancelled_cause(self):
        await self.init()
        async def block(uri, _):
            await asyncio.Event().wait()
        self.fake.after_write = block
        with self.assertRaises(asyncio.TimeoutError) as caught:
            await asyncio.wait_for(self.backend.store_experience(self.record(), timeout=30), timeout=0.02)
        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
        self.assertEqual(caught.exception.__cause__.source_id, next(iter(self.fake.resources)))
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_cancellation_during_original_confirmation_retains_locator(self):
        await self.init()
        started = asyncio.Event()
        async def block_download(method, kwargs):
            started.set()
            await asyncio.Event().wait()
        self.fake.handlers["/api/v1/content/download"] = block_download
        task = asyncio.create_task(self.backend.store_process(self.record(kind="task_process")))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.source_id, next(iter(self.fake.resources)))
        self.assertEqual(sum(path.endswith("/write") for _, path, _ in self.fake.calls), 1)

    async def test_close_waits_for_inflight_write_and_retains_confirmed_data(self):
        await self.init()
        started, release = asyncio.Event(), asyncio.Event()
        async def block(uri, _):
            started.set()
            await release.wait()
            return ok({"uri": uri, "vector_status": "complete"})
        self.fake.after_write = block
        writing = asyncio.create_task(self.backend.store_experience(self.record()))
        await started.wait()
        closing = asyncio.create_task(self.backend.close())
        await asyncio.sleep(0)
        self.assertFalse(self.fake.closed)
        self.assert_error(await self.backend.store_experience(self.record()), "NOT_READY")
        release.set()
        result = await writing
        self.assertTrue(result["ok"], result)
        self.assertIsNone(await closing)
        self.assertTrue(self.fake.closed)
        self.assertIn(result["data"]["source_id"], self.fake.resources)

    async def test_close_failure_is_sanitized_and_release_can_be_retried(self):
        await self.init()
        original = self.fake.aclose
        async def failure():
            raise RuntimeError("DO NOT EXPOSE")
        self.fake.aclose = failure
        self.assert_error(await self.backend.close(), "BACKEND_ERROR")
        self.fake.aclose = original
        self.assertIsNone(await self.backend.close())
        self.assertEqual((await self.backend.status())["data"]["state"], "closed")

    async def test_redirect_is_rejected_not_followed(self):
        self.fake.handlers["/health"] = lambda *_: FakeResponse(status=302, data={})
        self.assert_error(await self.backend.initialize(self.options), "BACKEND_ERROR")
        self.assertEqual(len(self.fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
