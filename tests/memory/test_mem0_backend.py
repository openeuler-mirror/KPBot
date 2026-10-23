"""mem0 adapter tests: stdlib unittest, fake HTTP only, no service/model.

Run: PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest tests.memory.test_mem0_backend -v
"""

import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest

_SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from kpbot_memory.backends.mem0_http import Mem0Backend

OPENAPI = {
    "openapi": "3.1.0",
    "components": {"schemas": {"MemoryCreate": {"properties": {"messages": {}, "user_id": {},
                                                               "metadata": {}, "infer": {}}}}},
}


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return copy.deepcopy(self.data)


class FakeClient:
    """Async request/aclose client matching the injected-client contract."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.closed = False

    async def request(self, method, path, timeout=None, json=None, **kwargs):
        self.requests.append({"method": method, "path": path, "json": json, "timeout": timeout})
        result = self.handler(method, path, json)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self):
        self.closed = True


def static_handler(responses):
    """responses: dict like {("POST", "/memories"): FakeResponse} with FIFO lists."""
    queues = {key: (list(v) if isinstance(v, list) else [v]) for key, v in responses.items()}

    def handler(method, path, json):
        if (method, path) in queues and queues[(method, path)]:
            return queues[(method, path)].pop(0)
        if (method, path) == ("GET", "/openapi.json"):
            return FakeResponse(data=OPENAPI)
        return FakeResponse(status=404, data={"detail": "not found"})

    return handler


BASE_OPTIONS = {"endpoint": "http://127.0.0.1:8888", "owner_id": "user-01",
                "max_input_bytes": 1048576, "derive_memories": True}


def record(kind="experience", project="project-a", task="task-101", content=None):
    return {"kind": kind, "project_id": project, "owner_id": "user-01", "task_id": task,
            "visibility": "private",
            "content": content if content is not None else {"summary": "示例经验", "conditions": ["小输入"], "evidence": ["证据"]}}


class Mem0BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_initialize_status_close(self):
        backend = Mem0Backend(client=FakeClient(static_handler({})))
        first = await backend.initialize(BASE_OPTIONS, timeout=5)
        self.assertTrue(first["ok"] and first["data"]["state"] == "ready", first)
        self.assertEqual(set(first["data"]), {"state", "message", "supports_shared"})
        self.assertFalse(first["data"]["supports_shared"])
        status = await backend.status(timeout=5)
        self.assertTrue(status["ok"] and status["data"]["state"] == "ready")
        again = await backend.initialize(dict(BASE_OPTIONS), timeout=5)
        self.assertTrue(again["ok"])
        changed = await backend.initialize({**BASE_OPTIONS, "endpoint": "http://127.0.0.1:9999"}, timeout=5)
        self.assertEqual(changed["error"]["code"], "INVALID_ARGUMENT")
        closed = await backend.close()
        self.assertIsNone(closed)
        after = await backend.status(timeout=5)
        self.assertEqual(after["data"]["state"], "closed")
        denied = await backend.store_experience(record(), timeout=5)
        self.assertEqual(denied["error"]["code"], "NOT_READY")

    async def test_uninitialized_data_methods_are_not_ready(self):
        backend = Mem0Backend(client=FakeClient(static_handler({})))
        result = await backend.store_experience(record(), timeout=5)
        self.assertEqual(result["error"]["code"], "NOT_READY")
        result = await backend.search({"query": "x", "features": [], "sources": [{"project_id": "p", "owner_id": "user-01"}], "kinds": ["experience"], "limit": 5}, timeout=5)
        self.assertEqual(result["error"]["code"], "NOT_READY")

    async def test_invalid_options(self):
        backend = Mem0Backend(client=FakeClient(static_handler({})))
        for bad in ({"owner_id": "u"}, {**BASE_OPTIONS, "endpoint": "ftp://x"}, {**BASE_OPTIONS, "endpoint": "http://u:p@h/"},
                    {**BASE_OPTIONS, "max_input_bytes": 0}, {**BASE_OPTIONS, "derive_memories": "yes"},
                    {**BASE_OPTIONS, "unknown": 1}, {"endpoint": "http://127.0.0.1:8888"}):
            result = await backend.initialize(bad, timeout=5)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT", bad)

    async def test_probe_requires_infer_in_openapi(self):
        no_infer = {"components": {"schemas": {"MemoryCreate": {"properties": {"messages": {}}}}}}
        backend = Mem0Backend(client=FakeClient(lambda m, p, j: FakeResponse(data=no_infer)))
        result = await backend.initialize(BASE_OPTIONS, timeout=5)
        self.assertEqual(result["error"]["code"], "UNAVAILABLE")

    async def test_connection_failure_is_unavailable(self):
        class Boom(Exception):
            pass
        backend = Mem0Backend(client=FakeClient(lambda m, p, j: Boom()))
        result = await backend.initialize(BASE_OPTIONS, timeout=5)
        self.assertEqual(result["error"]["code"], "UNAVAILABLE")

    async def _init(self, handler):
        backend = Mem0Backend(client=FakeClient(handler))
        result = await backend.initialize(BASE_OPTIONS, timeout=5)
        assert result["ok"], result
        return backend

    async def test_store_experience_roundtrip_confirm(self):
        rec = record()
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        calls = []

        def handler(method, path, payload):
            calls.append((method, path, payload))
            if (method, path) == ("GET", "/openapi.json"):
                return FakeResponse(data=OPENAPI)
            if (method, path) == ("POST", "/memories"):
                assert payload["infer"] is False
                assert payload["user_id"] == '["kpbot-mem0","raw","experience","user-01","project-a"]'
                assert payload["metadata"]["kpbot_kind"] == "experience"
                return FakeResponse(data={"results": [{"id": "m-1", "memory": payload["messages"][0]["content"], "event": "ADD"}]})
            if (method, path) == ("GET", "/memories/m-1"):
                return FakeResponse(data={"id": "m-1", "memory": text})
            raise AssertionError((method, path))

        backend = await self._init(handler)
        result = await backend.store_experience(rec, timeout=5)
        self.assertEqual(result, {"ok": True, "data": {"source_id": "m-1", "processing": "ready"}})
        self.assertEqual([c[1] for c in calls], ["/openapi.json", "/memories", "/memories/m-1"])

    async def test_store_experience_never_derives(self):
        rec = record()
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        def ok_handler(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                return FakeResponse(data={"results": [{"id": "m-1", "memory": text, "event": "ADD"}]})
            if (method, path) == ("GET", "/memories/m-1"):
                return FakeResponse(data={"id": "m-1", "memory": text})
            return FakeResponse(data=OPENAPI)

        backend = await self._init(ok_handler)
        result = await backend.store_experience(rec, timeout=5)
        self.assertTrue(result["ok"])
        posts = [r for r in backend._client.requests if r["path"] == "/memories" and r["method"] == "POST"]
        self.assertEqual(len(posts), 1)

    async def test_store_process_derives_in_isolated_namespace(self):
        messages = [{"role": "user", "content": "任务输入"}, {"role": "assistant", "content": "执行记录"}]
        rec = record(kind="task_process", content=messages)
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        def handler(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                if payload["infer"] is False:
                    return FakeResponse(data={"results": [{"id": "raw-1", "memory": text, "event": "ADD"}]})
                assert payload["infer"] is True
                assert payload["user_id"] == '["kpbot-mem0","derived","task_process","user-01","project-a"]'
                assert payload["metadata"]["kpbot_source_id"] == "raw-1"
                assert payload["messages"] == messages
                return FakeResponse(data={"results": [{"id": "d-1", "memory": "提炼事实", "event": "ADD"}]})
            if (method, path) == ("GET", "/memories/raw-1"):
                return FakeResponse(data={"id": "raw-1", "memory": text})
            return FakeResponse(data=OPENAPI)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        result = await backend.store_process(rec, timeout=5)
        self.assertEqual(result, {"ok": True, "data": {"source_id": "raw-1", "processing": "ready"}})

    async def test_store_process_derivation_failure_keeps_original(self):
        rec = record(kind="task_process", content="纯文本过程")
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        state = {"derive_failed": False}

        def handler(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                if payload["infer"] is False:
                    return FakeResponse(data={"results": [{"id": "raw-1", "memory": text, "event": "ADD"}]})
                state["derive_failed"] = True
                return FakeResponse(status=500, data={"detail": "boom"})
            if (method, path) == ("GET", "/memories/raw-1"):
                return FakeResponse(data={"id": "raw-1", "memory": text})
            return FakeResponse(data=OPENAPI)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        result = await backend.store_process(rec, timeout=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], {"source_id": "raw-1", "processing": "failed"})
        self.assertTrue(state["derive_failed"])

    async def test_store_process_derivation_off(self):
        rec = record(kind="task_process", content=[{"role": "user", "content": "x"}])
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        def handler(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                return FakeResponse(data={"results": [{"id": "raw-1", "memory": text, "event": "ADD"}]})
            if (method, path) == ("GET", "/memories/raw-1"):
                return FakeResponse(data={"id": "raw-1", "memory": text})
            return FakeResponse(data=OPENAPI)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize({**BASE_OPTIONS, "derive_memories": False}, timeout=5))["ok"])
        result = await backend.store_process(rec, timeout=5)
        self.assertEqual(result["data"]["processing"], "ready")
        posts = [r for r in backend._client.requests if r["path"] == "/memories" and r["method"] == "POST"]
        self.assertEqual(len(posts), 1)

    async def test_write_unknown_paths(self):
        rec = record()
        # add 500（已开始、无 locator）→ WRITE_UNKNOWN, source_id=None
        def handler500(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                return FakeResponse(status=500, data={"detail": "x"})
            return FakeResponse(data=OPENAPI)
        backend = Mem0Backend(client=FakeClient(handler500))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        result = await backend.store_experience(rec, timeout=5)
        self.assertEqual((result["error"]["code"], result["error"]["source_id"]), ("WRITE_UNKNOWN", None))

        # add 成功但读回不一致 → WRITE_UNKNOWN + source_id
        text = json.dumps(rec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        def handler_mismatch(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                return FakeResponse(data={"results": [{"id": "m-9", "memory": text, "event": "ADD"}]})
            if (method, path) == ("GET", "/memories/m-9"):
                return FakeResponse(data={"id": "m-9", "memory": "被篡改的内容"})
            return FakeResponse(data=OPENAPI)
        backend = Mem0Backend(client=FakeClient(handler_mismatch))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        result = await backend.store_experience(rec, timeout=5)
        self.assertEqual((result["error"]["code"], result["error"]["source_id"]), ("WRITE_UNKNOWN", "m-9"))

        # add 200 但无 ADD 结果 → WRITE_UNKNOWN
        def handler_noadd(method, path, payload):
            if (method, path) == ("POST", "/memories"):
                return FakeResponse(data={"results": []})
            return FakeResponse(data=OPENAPI)
        backend = Mem0Backend(client=FakeClient(handler_noadd))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        result = await backend.store_experience(rec, timeout=5)
        self.assertEqual(result["error"]["code"], "WRITE_UNKNOWN")

    async def test_store_validation_and_too_large(self):
        backend = Mem0Backend(client=FakeClient(static_handler({})))
        self.assertTrue((await backend.initialize({**BASE_OPTIONS, "max_input_bytes": 64}, timeout=5))["ok"])
        for bad, code in (
            ({"kind": "experience"}, "INVALID_ARGUMENT"),
            ({**record(), "kind": "task_process"}, "INVALID_ARGUMENT"),
            ({**record(), "visibility": "shared"}, "UNSUPPORTED"),
            ({**record(), "owner_id": "user-02"}, "ACCESS_DENIED"),
            ({**record(), "task_id": " "}, "INVALID_ARGUMENT"),
        ):
            result = await backend.store_experience(bad, timeout=5)
            self.assertEqual(result["error"]["code"], code, bad)
        result = await backend.store_experience(record(content="x" * 200), timeout=5)
        self.assertEqual(result["error"]["code"], "TOO_LARGE")

    async def test_search_namespaces_merge_and_mapping(self):
        rec_a = record(project="project-a", task="t-a")
        rec_b = record(kind="task_process", project="project-b", task="t-b", content=[{"role": "user", "content": "过程"}])
        text_a = json.dumps(rec_a, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        text_b = json.dumps(rec_b, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        def search_response(items):
            return FakeResponse(data={"results": items})

        def handler(method, path, payload):
            if (method, path) != ("POST", "/search"):
                return FakeResponse(data=OPENAPI)
            ns = payload["user_id"]
            if ns == '["kpbot-mem0","raw","experience","user-01","project-a"]':
                return search_response([
                    {"id": "m-a", "memory": text_a, "score": 0.42,
                     "metadata": {"kpbot_kind": "experience", "project_id": "project-a", "owner_id": "user-01", "task_id": "t-a"}},
                    {"id": "m-junk", "memory": "不是JSON的噪音", "score": 0.9, "metadata": {}},
                ])
            if ns == '["kpbot-mem0","raw","task_process","user-01","project-b"]':
                return search_response([
                    {"id": "m-b", "memory": text_b, "score": 0.91,
                     "metadata": {"kpbot_kind": "task_process", "project_id": "project-b", "owner_id": "user-01", "task_id": "t-b"}},
                ])
            if ns == '["kpbot-mem0","derived","task_process","user-01","project-b"]':
                return search_response([
                    {"id": "m-d", "memory": "提炼出的结论", "score": 0.55,
                     "metadata": {"kpbot_kind": "task_process", "project_id": "project-b", "owner_id": "user-01",
                                  "task_id": "t-b", "kpbot_source_id": "m-b"}},
                ])
            if ns == '["kpbot-mem0","derived","task_process","user-01","project-a"]':
                return search_response([])
            if ns == '["kpbot-mem0","raw","experience","user-01","project-b"]':
                return search_response([])
            if ns == '["kpbot-mem0","raw","task_process","user-01","project-a"]':
                return search_response([])
            raise AssertionError(ns)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        request = {"query": "循环 重复搬移", "features": ["寄存器压力"],
                   "sources": [{"project_id": "project-a", "owner_id": "user-01"},
                               {"project_id": "project-b", "owner_id": "user-01"}],
                   "kinds": ["experience", "task_process"], "limit": 5}
        result = await backend.search(request, timeout=5)
        self.assertTrue(result["ok"], result)
        items, partial = result["data"]["items"], result["data"]["partial"]
        self.assertTrue(partial)  # m-junk 被跳过
        # 相似度降序（2.2.1 契约：score 越大越相关）；derived 命中按 kpbot_source_id 归因到其原始记录 m-b
        self.assertEqual([item["source_id"] for item in items], ["m-b", "m-b", "m-a"])
        self.assertEqual(items[0]["content"], rec_b["content"])
        self.assertEqual(items[0]["content_type"], "original")
        self.assertEqual(items[1]["content_type"], "derived")
        self.assertEqual(items[1]["source_id"], "m-b")
        self.assertEqual(items[2]["content"], rec_a["content"])
        self.assertEqual(items[2]["content_type"], "original")
        searched = [r["json"]["user_id"] for r in backend._client.requests if r["path"] == "/search"]
        self.assertEqual(len(searched), 6)  # 2 项目 × (2 raw kind + 1 derived)
        self.assertEqual(backend._client.requests[-1]["json"]["query"], "循环 重复搬移\n寄存器压力")

    async def test_search_all_hits_unreadable_is_read_failed(self):
        noise = [{"id": f"m-{i}", "memory": "噪音", "score": 0.1, "metadata": {}} for i in range(3)]

        def handler(method, path, payload):
            if (method, path) == ("POST", "/search"):
                return FakeResponse(data={"results": noise})
            return FakeResponse(data=OPENAPI)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        request = {"query": "q", "features": [], "sources": [{"project_id": "p", "owner_id": "user-01"}],
                   "kinds": ["experience"], "limit": 2}
        result = await backend.search(request, timeout=5)
        self.assertEqual(result["error"]["code"], "READ_FAILED")  # 全部命中不可读

    async def test_search_source_failure_is_error_and_validation(self):
        def handler(method, path, payload):
            if (method, path) == ("POST", "/search"):
                return FakeResponse(status=500, data={"detail": "x"})
            return FakeResponse(data=OPENAPI)

        backend = Mem0Backend(client=FakeClient(handler))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        request = {"query": "q", "features": [], "sources": [{"project_id": "p", "owner_id": "user-01"}],
                   "kinds": ["experience"], "limit": 5}
        result = await backend.search(request, timeout=5)
        self.assertEqual(result["error"]["code"], "BACKEND_ERROR")

        for bad, code in (
            ({"query": "", "features": [], "sources": [{"project_id": "p", "owner_id": "user-01"}], "kinds": ["experience"], "limit": 5}, "INVALID_ARGUMENT"),
            ({**request, "include_shared": True}, "UNSUPPORTED"),
            ({**request, "kinds": []}, "INVALID_ARGUMENT"),
            ({**request, "limit": 21}, "INVALID_ARGUMENT"),
            ({**request, "sources": [{"project_id": "p", "owner_id": "user-02"}]}, "ACCESS_DENIED"),
        ):
            result = await backend.search(bad, timeout=5)
            self.assertEqual(result["error"]["code"], code, bad)

    async def test_close_is_idempotent_and_releases_client(self):
        backend = Mem0Backend(client=FakeClient(static_handler({})))
        self.assertTrue((await backend.initialize(BASE_OPTIONS, timeout=5))["ok"])
        client = backend._client
        self.assertIsNone(await backend.close())
        self.assertTrue(client.closed)
        self.assertIsNone(await backend.close())

    async def test_timeout_before_execution(self):
        class SlowClient:
            async def request(self, method, path, timeout=None, json=None, **kwargs):
                await asyncio.sleep(10)
                return FakeResponse(data=OPENAPI)

            async def aclose(self):
                pass

        backend = Mem0Backend(client=SlowClient())
        result = await backend.initialize(BASE_OPTIONS, timeout=0.05)
        self.assertFalse(result["ok"])


class Mem0ConfigIntegrationTests(unittest.TestCase):
    def test_config_roundtrip_and_create_backend(self):
        import tempfile, os, stat
        from kpbot_memory.backend_config import load_config
        from kpbot_memory.backend_cli import create_backend
        config = {"backend": "mem0", "user_id": "user-01", "current_project": "project-a",
                  "allowed_projects": ["project-a", "project-b"],
                  "options": {"endpoint": "http://127.0.0.1:8888", "derive_memories": True}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            loaded = load_config(path)
            self.assertEqual(loaded["backend"], "mem0")
            self.assertIsInstance(create_backend("mem0"), Mem0Backend)

    def test_config_rejects_unknown_mem0_options_and_wrong_types(self):
        from kpbot_memory.backend_config import validate_config
        from kpbot_memory.backend_config import BackendConfigError
        base = {"backend": "mem0", "user_id": "u", "current_project": "p",
                "allowed_projects": ["p"], "options": {"endpoint": "http://127.0.0.1:8888"}}
        with self.assertRaises(BackendConfigError) as ctx:
            validate_config({**base, "options": {**base["options"], "owner_id": "u"}})
        self.assertIn("unknown mem0 backend option", str(ctx.exception))
        with self.assertRaises(BackendConfigError):
            validate_config({**base, "options": {**base["options"], "derive_memories": "yes"}})


if __name__ == "__main__":
    unittest.main()
