"""TencentDB MemoryCore adapter security tests: malicious inputs, poisoned
storage, provenance forgery and information-leak checks. Stdlib unittest only.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_tencentdb_security -v
"""

import asyncio
import copy
import hashlib
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

SECRET_MARKER = "TOP-SECRET-TOKEN-DO-NOT-EXPOSE"
SCOPE = ("project-a", "kpbot", "kpbot-user")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def envelope_text(source_id, record, *, sha=None):
    digest = sha or hashlib.sha256(canonical(record)).hexdigest()
    return canonical({"schema_version": 1, "source_id": source_id, "record": record,
                      "record_sha256": digest}).decode("utf-8")


def chunk_contents(source_id, kind, text, size=8000):
    parts = [text[index:index + size] for index in range(0, len(text), size)]
    return [f"[[kpbot-memory:1:{source_id}:{kind}:{index}:{len(parts)}]]\n{part}"
            for index, part in enumerate(parts)]


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


def error_envelope(code=400, message="DO NOT EXPOSE: " + SECRET_MARKER):
    return FakeResponse(code, {"code": code, "message": message, "request_id": "req-1"})


class FakeClient:
    """Minimal MemoryCore gateway with direct storage poisoning hooks."""

    def __init__(self):
        self.store = {}
        self.calls = []
        self.handlers = {}
        self.health = {"status": "ok", "stores": {"vectorStore": True,
                                                  "embeddingService": False,
                                                  "ftsAvailable": True}}

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
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
        scope = (body.get("team_id", "default"), body.get("agent_id", "default"),
                 body.get("user_id", "default"))
        sessions = self.store.setdefault(scope, {})
        if path == "/v3/conversation/add":
            stored = sessions.setdefault(body.get("session_id", "default"), [])
            for message in body.get("messages", []):
                stored.append({"id": "msg-" + uuid.uuid4().hex, "role": message["role"],
                               "content": message["content"],
                               "timestamp": "2026-01-01T00:00:00Z",
                               "task_id": body.get("task_id")})
            return envelope({"accepted_ids": [], "accepted_versions": [], "total_count": len(stored)})
        if path == "/v3/conversation/query":
            rows = list(sessions.get(body.get("session_id"), []))
            offset, limit = body.get("offset", 0), body.get("limit", 20)
            page = rows[offset:offset + limit]
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
            return envelope({"messages": hits[:body.get("limit", 5)]})
        if path == "/v3/conversation/count":
            return envelope({"total": sum(len(session) for session in sessions.values())})
        raise AssertionError("Unexpected native API path: " + path)

    async def aclose(self):
        pass

    def seed(self, scope, source_id, contents):
        rows = self.store.setdefault(scope, {}).setdefault(source_id, [])
        for content in contents:
            rows.append({"id": "msg-" + uuid.uuid4().hex, "role": "user", "content": content,
                         "timestamp": "2026-01-01T00:00:00Z", "task_id": None})


class TencentDBSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.backend = SouthboundMemory(client=self.fake)
        self.options = {"api_key": "secret-key-" + SECRET_MARKER, "service_id": "default",
                        "owner_id": "kpbot-user"}
        self.query = {"query": "循环 优化", "features": [],
                      "sources": [{"project_id": "project-a", "owner_id": "kpbot-user"}],
                      "kinds": ["experience", "task_process"], "limit": 5}

    def record(self, *, content=None, **changes):
        data = {"kind": "experience", "project_id": "project-a", "owner_id": "kpbot-user",
                "task_id": "task-101", "visibility": "private",
                "content": {"text": "循环 优化"} if content is None else content}
        data.update(changes)
        return data

    async def ready(self, **changes):
        result = await self.backend.initialize({**self.options, **changes})
        self.assertTrue(result["ok"], result)
        return result

    async def search(self, **changes):
        return await self.backend.search({**self.query, **changes}, timeout=30)

    def assert_poison_excluded(self, result, *markers):
        """A poisoned hit must never surface; per contract the only-hit-failed
        case reports READ_FAILED instead of returning or echoing the content."""
        dumped = json.dumps(result, ensure_ascii=False)
        for marker in markers:
            self.assertNotIn(marker, dumped)
        if result["ok"]:
            self.assertEqual(result["data"]["items"], [])
        else:
            self.assertEqual(result["error"]["code"], "READ_FAILED")

    # ── input validation attacks ───────────────────────────────────────────

    async def test_oversized_input_rejected_without_any_write(self):
        await self.ready(max_input_bytes=256)
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "TOO_LARGE")
        self.assertIn("256", result["error"]["message"])
        self.assertEqual([call for call in self.fake.calls
                          if call[1] == "/v3/conversation/add"], [])

    async def test_input_at_exact_limit_is_stored(self):
        reference = envelope_text("kpbot-mem-" + "0" * 32,
                                  json.loads(canonical(self.record())))
        await self.ready(max_input_bytes=len(reference.encode("utf-8")))
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.assertTrue(result["ok"], result)

    async def test_deeply_nested_content_is_handled_without_crash(self):
        await self.ready()
        nested = current = {}
        for _ in range(200):
            current["child"] = {}
            current = current["child"]
        result = await self.backend.store_experience(self.record(content=nested), timeout=30)
        self.assertIn(result["error"]["code"] if not result["ok"] else "stored",
                      ("INVALID_ARGUMENT", "WRITE_UNKNOWN", "stored"))
        self.assertNotEqual(result.get("ok", False), None)

    async def test_non_json_values_are_rejected(self):
        await self.ready()
        for value in (float("nan"), float("inf"), float("-inf"), (1, 2), b"bytes",
                      {1: "nonstring key"}):
            record = self.record(content=value if isinstance(value, dict) else {"v": value})
            if isinstance(value, dict):
                record = self.record(content=value)
            result = await self.backend.store_experience(record, timeout=30)
            self.assertEqual(result["ok"], False, value)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT", value)

    async def test_identity_forgery_is_denied(self):
        await self.ready()
        result = await self.backend.store_experience(self.record(owner_id="victim"), timeout=30)
        self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
        result = await self.backend.store_experience(self.record(visibility="team"), timeout=30)
        self.assertEqual(result["error"]["code"], "UNSUPPORTED")
        forged = {**self.query,
                  "sources": [{"project_id": "project-a", "owner_id": "victim"}]}
        result = await self.backend.search(forged, timeout=30)
        self.assertEqual(result["error"]["code"], "ACCESS_DENIED")

    async def test_options_injection_is_rejected(self):
        for changes in ({"api_key": "x\r\nX-Injected: 1"},
                        {"api_key": "x\u0000y"},
                        {"endpoint": "http://user:pass@evil.example"},
                        {"endpoint": "http://evil.example/?leak=1"},
                        {"endpoint": "http://evil.example/#frag"},
                        {"endpoint": "http://evil.example/path"},
                        {"endpoint": "ftp://evil.example"},
                        {"endpoint": "javascript:alert(1)"},
                        {"agent_id": "x\u001by"},
                        {"service_id": "bad\nid"}):
            result = await self.backend.initialize({**self.options, **changes})
            self.assertEqual(result["ok"], False, changes)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT", changes)

    # ── poisoned storage attacks ───────────────────────────────────────────

    async def test_forged_chunk_header_never_returns_content(self):
        await self.ready()
        evil = '{"evil":"循环 优化 attack"}'
        self.fake.seed(SCOPE, "kpbot-mem-" + "f" * 32,
                       ["[[kpbot-memory:1:kpbot-mem-" + "f" * 32 + ":experience:0:1]]\n"
                        + evil])
        result = await self.search()
        self.assert_poison_excluded(result, "attack")

    async def test_tampered_integrity_hash_is_not_returned(self):
        await self.ready()
        record = self.record()
        sid = "kpbot-mem-" + "a" * 32
        text = envelope_text(sid, record, sha="0" * 64)
        self.fake.seed(SCOPE, sid, chunk_contents(sid, "experience", text))
        result = await self.search()
        self.assert_poison_excluded(result)

    async def test_provenance_forgery_across_projects_is_not_returned(self):
        await self.ready()
        record = self.record(project_id="victim-project")
        sid = "kpbot-mem-" + "b" * 32
        text = envelope_text(sid, record)
        self.fake.seed(SCOPE, sid, chunk_contents(sid, "experience", text))
        result = await self.search()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["items"], [])
        self.assertEqual(result["data"]["partial"], False)

    async def test_duplicate_json_keys_in_envelope_are_rejected(self):
        await self.ready()
        sid = "kpbot-mem-" + "c" * 32
        record = self.record()
        raw = ('{"record":' + canonical(record).decode("utf-8") +
               ',"record":{"kind":"task_process"},"record_sha256":"' +
               hashlib.sha256(canonical(record)).hexdigest() +
               '","schema_version":1,"source_id":"' + sid + '"}')
        self.fake.seed(SCOPE, sid, chunk_contents(sid, "experience", raw))
        result = await self.search()
        self.assert_poison_excluded(result)

    async def test_nonfinite_constants_in_envelope_are_rejected(self):
        await self.ready()
        sid = "kpbot-mem-" + "d" * 32
        record = self.record()
        raw = ('{"record":' + canonical(record).decode("utf-8") +
               ',"record_sha256":"' + hashlib.sha256(canonical(record)).hexdigest() +
               '","schema_version":1,"source_id":"' + sid + '","x":NaN}')
        self.fake.seed(SCOPE, sid, chunk_contents(sid, "experience", raw))
        result = await self.search()
        self.assert_poison_excluded(result)

    async def test_unknown_schema_version_is_rejected(self):
        await self.ready()
        sid = "kpbot-mem-" + "e" * 32
        record = self.record()
        raw = ('{"record":' + canonical(record).decode("utf-8") +
               ',"record_sha256":"' + hashlib.sha256(canonical(record)).hexdigest() +
               '","schema_version":2,"source_id":"' + sid + '"}')
        self.fake.seed(SCOPE, sid, chunk_contents(sid, "experience", raw))
        result = await self.search()
        self.assert_poison_excluded(result)

    async def test_inconsistent_chunk_stream_is_rejected(self):
        await self.ready()
        record = self.record()
        sid = "kpbot-mem-" + "1" * 32
        text = envelope_text(sid, record)
        parts = chunk_contents(sid, "experience", text, size=32)
        self.fake.seed(SCOPE, sid, [parts[0], parts[0]])
        result = await self.search()
        self.assert_poison_excluded(result)

        sid = "kpbot-mem-" + "2" * 32
        text = envelope_text(sid, record)
        parts = chunk_contents(sid, "experience", text, size=32)
        overflow = re.sub(r":0:(%d)" % len(parts), ":%d:%d" % (len(parts) + 5, len(parts)),
                          parts[0])
        self.fake.seed(SCOPE, sid, [overflow, parts[1]])
        result = await self.search()
        self.assert_poison_excluded(result)

        sid = "kpbot-mem-" + "3" * 32
        text = envelope_text(sid, record)
        parts = chunk_contents(sid, "experience", text, size=32)
        mismatched = parts[1].replace(":" + str(len(parts)) + "]]", ":9]]")
        self.fake.seed(SCOPE, sid, [parts[0], mismatched])
        result = await self.search()
        self.assert_poison_excluded(result)

    # ── information-leak checks ────────────────────────────────────────────

    async def test_error_messages_never_leak_native_bodies_or_credentials(self):
        await self.ready()
        for status in (400, 401, 403, 404, 413, 422, 429, 500, 503):
            self.fake.handlers["/v3/conversation/add"] = (
                lambda method, kwargs, status=status: error_envelope(status))
            result = await self.backend.store_experience(self.record(), timeout=30)
            self.assertNotIn(result["ok"], (None,))
            if not result["ok"]:
                message = result["error"]["message"]
                self.assertNotIn(SECRET_MARKER, message)
                self.assertNotIn(self.options["api_key"], message)
        self.fake.handlers["/v3/conversation/add"] = lambda method, kwargs: ConnectionError(
            "connection refused to " + SECRET_MARKER)
        result = await self.backend.store_experience(self.record(), timeout=30)
        self.assertNotIn(SECRET_MARKER, result["error"]["message"])
        self.assertNotIn(self.options["api_key"], result["error"]["message"])

    async def test_content_fidelity_roundtrip(self):
        await self.ready()
        content = {"empty": "", "none": None, "bool": True, "int": 9007199254740993,
                   "float": 1.25, "emoji": "😀👨‍👩‍👧", "cjk": "中文内容",
                   "embedded": "循环 优化", "nested": {"deep": [{"x": "🎉"}]}}
        stored = await self.backend.store_experience(self.record(content=content), timeout=30)
        self.assertTrue(stored["ok"], stored)
        result = await self.search()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(result["data"]["items"][0]["content"], content)

    async def test_chunk_boundary_splits_inside_multibyte_sequences(self):
        await self.ready(chunk_chars=512)
        content = "循环 优化 " + "😀👨‍👩‍👧中文🎉" * 200
        stored = await self.backend.store_experience(self.record(content=content), timeout=30)
        self.assertTrue(stored["ok"], stored)
        sid = stored["data"]["source_id"]
        rows = self.fake.store[SCOPE][sid]
        self.assertGreater(len(rows), 1)
        result = await self.search()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["items"][0]["content"], content)

    # ── resource / parameter attacks ───────────────────────────────────────

    async def test_search_parameter_boundaries(self):
        await self.ready()
        for changes in ({"limit": 21}, {"limit": 0}, {"limit": -1}, {"limit": "5"},
                        {"limit": True}, {"limit": 5.5}):
            result = await self.search(**changes)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT", changes)
        result = await self.search(query="", features=[])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    async def test_timeout_parameter_attacks(self):
        await self.ready()
        for timeout in (0, -1, float("nan"), float("inf"), "30", None):
            result = await self.backend.store_experience(self.record(), timeout=timeout)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
            result = await self.backend.search(self.query, timeout=timeout)
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    async def test_overlong_query_is_truncated_not_failed(self):
        await self.ready()
        result = await self.search(query="循环 " + "x" * 5000)
        self.assertTrue(result["ok"], result)
        searches = [call for call in self.fake.calls if call[1] == "/v3/conversation/search"]
        self.assertTrue(searches)
        self.assertLessEqual(len(searches[-1][2]["json"]["query"]), 2048)

    async def test_forged_header_from_foreign_owner_scope_is_invisible(self):
        await self.ready()
        record = self.record(owner_id="victim")
        sid = "kpbot-mem-" + "9" * 32
        text = envelope_text(sid, record)
        victim_scope = ("project-a", "kpbot", "victim")
        self.fake.seed(victim_scope, sid, chunk_contents(sid, "experience", text))
        result = await self.search()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
