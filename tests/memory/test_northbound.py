"""Five-operation facade tests with injected async fakes; never start services.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.memory.test_northbound -v
"""
from __future__ import annotations

import asyncio
import copy
import inspect
from pathlib import Path
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "Plugins/memory/skills/kpbot-memory/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from kpbot_memory.northbound import NorthboundMemory  # noqa: E402


def config():
    return {"backend": "openviking", "user_id": "local-user", "current_project": "p1",
            "allowed_projects": ["p1", "p2"], "options": {"path": "explicit", "nested": {"key": "value"}}}


def failure(code="UNAVAILABLE", source_id=None):
    return {"ok": False, "error": {"code": code, "message": "backend failure", "source_id": source_id}}


def ready(state="ready"):
    return {"ok": True, "data": {"state": state, "message": "", "supports_shared": False}}


def record(kind="experience"):
    return {"kind": kind, "project_id": "p1", "task_id": "task-1",
            "content": {"conditions": ["条件保持"], "conclusion": "原结论", "evidence": ["原证据"], "number": 1.0}}


def item(**changes):
    result = {"content": {"complete": "原始全文"}, "content_type": "original", "kind": "experience",
              "project_id": "p1", "owner_id": "local-user", "task_id": "task-1", "source_id": "source-1", "score": 0.72}
    result.update(changes)
    return result


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.replies = {}
        self.hooks = {}
        self.state = "uninitialized"

    async def invoke(self, name, payload=None, timeout=None):
        self.calls.append((name, copy.deepcopy(payload), timeout))
        if name in self.hooks:
            result = self.hooks[name](payload)
            return await result if inspect.isawaitable(result) else result
        if name in self.replies:
            return copy.deepcopy(self.replies[name])
        if name == "initialize":
            self.state = "ready"
            return ready()
        if name == "status":
            return ready(self.state)
        if name in ("store_experience", "store_process"):
            return {"ok": True, "data": {"source_id": "source-1", "processing": "unknown"}}
        if name == "search":
            return {"ok": True, "data": {"items": [], "partial": False}}
        if name == "close":
            self.state = "closed"
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


class NorthboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.memory = NorthboundMemory(self.backend)

    def assertError(self, result, code):
        self.assertEqual(set(result), {"ok", "error"})
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error"]["code"], code)
        self.assertEqual(set(result["error"]), {"code", "message", "source_id"})

    async def init(self):
        self.assertEqual(await self.memory.initialize(config()), ready())

    async def test_status_before_initialize_and_after_close(self):
        self.assertEqual(await self.memory.status(), ready("uninitialized"))
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(await self.memory.close())
        self.assertEqual(await self.memory.status(), ready("closed"))
        self.assertIsNone(await self.memory.close())
        self.assertEqual([call[0] for call in self.backend.calls], ["close"])

    async def test_data_operations_require_successful_initialization(self):
        for request in (record(), record("task_process")):
            self.assertError(await self.memory.remember(request), "NOT_READY")
        self.assertError(await self.memory.recall({"query": "question"}), "NOT_READY")
        self.assertEqual(self.backend.calls, [])

    async def test_initialize_passes_only_backend_options_and_timeout(self):
        original = config()
        result = await self.memory.initialize(original, timeout=2.5)
        self.assertEqual(result, ready())
        name, options, timeout = self.backend.calls[-1]
        self.assertEqual(name, "initialize")
        self.assertEqual(options, {**original["options"], "owner_id": original["user_id"]})
        self.assertNotIn("owner_id", original["options"])
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 2.5)
        self.assertNotIn("user_id", options)
        self.assertEqual(await self.memory.status(), ready())

    async def test_initialize_config_is_immutable_and_business_payload_never_changes_identity(self):
        original = config()
        await self.memory.initialize(original)
        original["user_id"] = "intruder"
        original["allowed_projects"].append("p3")
        original["options"]["nested"]["key"] = "changed"
        result = await self.memory.remember({**record(), "owner_id": "intruder"})
        self.assertError(result, "ACCESS_DENIED")
        result = await self.memory.recall({"query": "q", "project_ids": ["p3"]})
        self.assertError(result, "ACCESS_DENIED")

    async def test_repeat_initialization_same_config_and_reject_changed_config(self):
        await self.init()
        self.assertEqual(await self.memory.initialize(config()), ready())
        changed = config()
        changed["current_project"] = "p2"
        self.assertError(await self.memory.initialize(changed), "INVALID_ARGUMENT")
        self.assertEqual(sum(name == "initialize" for name, _, _ in self.backend.calls), 2)
        self.assertEqual(await self.memory.status(), ready())

    async def test_initialize_failure_is_passthrough_and_retry_allowed(self):
        expected = failure("UNAVAILABLE")
        self.backend.replies["initialize"] = expected
        self.assertEqual(await self.memory.initialize(config()), expected)
        self.assertError(await self.memory.remember(record()), "NOT_READY")
        # A standalone backend status must not bypass failed initialization.
        self.backend.replies["status"] = ready()
        self.assertEqual((await self.memory.status())["data"]["state"], "unavailable")
        del self.backend.replies["initialize"]
        self.assertEqual(await self.memory.initialize(config()), ready())
        self.assertTrue((await self.memory.remember(record()))["ok"])

    async def test_backend_owner_must_match_trusted_user_not_provider_identity(self):
        for owner in ("other-user", None, ""):
            candidate = config()
            candidate["options"]["owner_id"] = owner
            self.assertError(await self.memory.initialize(candidate), "ACCESS_DENIED")
        self.assertEqual(self.backend.calls, [])
        candidate = config()
        candidate["options"].update(owner_id="local-user", user_id="provider-user")
        self.assertEqual(await self.memory.initialize(candidate), ready())
        self.assertEqual(self.backend.calls[-1][1]["user_id"], "provider-user")
        self.assertEqual(self.backend.calls[-1][1]["owner_id"], "local-user")

    async def test_implicit_and_explicit_same_backend_owner_are_equivalent_configs(self):
        await self.init()
        candidate = config()
        candidate["options"]["owner_id"] = candidate["user_id"]
        self.assertEqual(await self.memory.initialize(candidate), ready())

    async def test_invalid_config_rejected_before_backend(self):
        for invalid in ({}, None, {**config(), "user_id": ""}, {**config(), "roles": ["read"]}):
            with self.subTest(invalid=invalid):
                self.assertError(await self.memory.initialize(invalid), "INVALID_ARGUMENT")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(await self.memory.status(), ready("uninitialized"))

    async def test_closed_instance_cannot_reinitialize_or_access_data(self):
        await self.init()
        await self.memory.close()
        self.assertError(await self.memory.initialize(config()), "NOT_READY")
        self.assertError(await self.memory.remember(record()), "NOT_READY")
        self.assertError(await self.memory.recall({"query": "q"}), "NOT_READY")
        self.assertEqual(await self.memory.status(), ready("closed"))

    async def test_remember_routes_both_kinds_and_supplies_private_identity(self):
        await self.init()
        for kind, method in (("experience", "store_experience"), ("task_process", "store_process")):
            original = record(kind)
            expected = {"ok": True, "data": {"source_id": "source-1", "processing": "unknown"}}
            self.assertEqual(await self.memory.remember(original), expected)
            name, sent, timeout = self.backend.calls[-1]
            self.assertEqual(name, method)
            self.assertEqual(sent, {**original, "owner_id": "local-user", "visibility": "private"})
            self.assertEqual(original, record(kind))
            self.assertGreater(timeout, 0)

    async def test_content_text_object_array_preserved_and_independent(self):
        await self.init()
        content_values = ["\n原始输入\n工具完整结果\n", {"summary": "原文", "evidence": [1, 1.0, None]},
                          [{"role": "user", "content": "完整输入"}, {"role": "tool", "content": ["a", "b"]}], ""]
        for content in content_values:
            original = {**record("task_process"), "content": copy.deepcopy(content)}
            baseline = copy.deepcopy(original)

            def mutate(payload):
                self.assertEqual(payload["content"], content)
                payload["content"] = "backend-only change"
                return {"ok": True, "data": {"source_id": "s", "processing": "pending"}}

            self.backend.hooks["store_process"] = mutate
            self.assertEqual((await self.memory.remember(original))["data"]["processing"], "pending")
            self.assertEqual(original, baseline)

    async def test_remember_rejects_identity_scope_and_sharing(self):
        await self.init()
        for fields, code in (({"owner_id": "other"}, "ACCESS_DENIED"), ({"project_id": "other"}, "ACCESS_DENIED"),
                             ({"visibility": "shared"}, "UNSUPPORTED"), ({"visibility": None}, "UNSUPPORTED")):
            with self.subTest(fields=fields):
                self.assertError(await self.memory.remember({**record(), **fields}), code)
        self.assertEqual([name for name, _, _ in self.backend.calls], ["initialize"])

    async def test_remember_invalid_record_fields_and_non_json_content(self):
        await self.init()
        cases = [None, [], {}, {**record(), "kind": "unknown"}, {**record(), "kind": []},
                 {**record(), "project_id": ""}, {**record(), "task_id": "  "}, {**record(), "owner_id": None},
                 {**record(), "content": None}, {**record(), "content": 3}, {**record(), "content": {"x": float("nan")}},
                 {**record(), "content": {1: "not-json"}}, {**record(), "user_id": "intruder"}]
        for field in ("project_id", "task_id", "kind", "content"):
            candidate = record()
            del candidate[field]
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=type(candidate)):
                self.assertError(await self.memory.remember(candidate), "INVALID_ARGUMENT")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_review_business_structure_is_not_rewritten_or_enforced(self):
        await self.init()
        original = {**record(), "content": {"approved_by_kpbot": True, "evidence": "完整自定义证据"}}
        self.assertTrue((await self.memory.remember(original))["ok"])
        self.assertEqual(self.backend.calls[-1][1]["content"], original["content"])

    async def test_processing_states_and_write_error_source_passthrough_without_retry(self):
        await self.init()
        for state in ("ready", "pending", "failed", "unknown"):
            expected = {"ok": True, "data": {"source_id": "native://s", "processing": state}}
            self.backend.replies["store_experience"] = expected
            self.assertEqual(await self.memory.remember(record()), expected)
        for code in ("TOO_LARGE", "WRITE_UNKNOWN", "UNAVAILABLE"):
            expected = failure(code, "known-source")
            self.backend.replies["store_experience"] = expected
            before = len(self.backend.calls)
            self.assertEqual(await self.memory.remember(record()), expected)
            self.assertEqual(len(self.backend.calls), before + 1)

    async def test_unconfirmed_write_shape_returns_write_unknown_preserving_known_source(self):
        await self.init()
        for value in (None, {}, {"ok": True, "data": {"source_id": "known"}},
                      {"ok": True, "data": {"source_id": "known", "processing": "fictional"}},
                      {"ok": False, "error": {"code": "WRITE_UNKNOWN", "source_id": "known"}}):
            self.backend.replies["store_experience"] = value
            result = await self.memory.remember(record())
            self.assertError(result, "WRITE_UNKNOWN")
            if isinstance(value, dict) and value:
                self.assertEqual(result["error"]["source_id"], "known")

    async def test_recall_defaults_form_narrow_southbound_scope(self):
        await self.init()
        self.assertEqual(await self.memory.recall({"query": "原查询"}), {"ok": True, "data": {"items": [], "partial": False}})
        self.assertEqual(self.backend.calls[-1][1], {
            "query": "原查询", "features": [], "sources": [{"project_id": "p1", "owner_id": "local-user"}],
            "kinds": ["experience", "task_process"], "limit": 5})

    async def test_recall_multiple_projects_kind_and_features_are_not_reinterpreted(self):
        await self.init()
        request = {"query": "  原始查询\n", "features": ["寄存器压力", "不必字面匹配"],
                   "project_ids": ["p2", "p1"], "include_shared": False, "kinds": ["task_process"], "limit": 20}
        self.assertTrue((await self.memory.recall(request))["ok"])
        sent = self.backend.calls[-1][1]
        self.assertEqual(sent["query"], request["query"])
        self.assertEqual(sent["features"], request["features"])
        self.assertEqual(sent["sources"], [{"project_id": "p2", "owner_id": "local-user"}, {"project_id": "p1", "owner_id": "local-user"}])
        self.assertEqual(sent["kinds"], ["task_process"])
        self.assertEqual(sent["limit"], 20)
        self.assertNotIn("include_shared", sent)

    async def test_feature_only_query_accepted(self):
        await self.init()
        self.assertTrue((await self.memory.recall({"features": ["feature"]}))["ok"])
        self.assertEqual(self.backend.calls[-1][1]["query"], "")

    async def test_duplicate_projects_and_kinds_do_not_expand_scope(self):
        await self.init()
        await self.memory.recall({"query": "q", "project_ids": ["p2", "p2", "p1"], "kinds": ["experience", "experience"]})
        self.assertEqual(len(self.backend.calls[-1][1]["sources"]), 2)
        self.assertEqual(self.backend.calls[-1][1]["kinds"], ["experience"])

    async def test_recall_sharing_and_caller_sources_identity_denied(self):
        await self.init()
        for fields, code in (({"include_shared": True}, "UNSUPPORTED"), ({"project_ids": ["other"]}, "ACCESS_DENIED"),
                             ({"owner_id": "other"}, "ACCESS_DENIED"), ({"sources": []}, "ACCESS_DENIED"),
                             ({"user_id": "other"}, "ACCESS_DENIED")):
            with self.subTest(fields=fields):
                self.assertError(await self.memory.recall({"query": "q", **fields}), code)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_recall_bad_arguments_and_unknown_fields_are_not_silently_coerced(self):
        await self.init()
        cases = [None, [], {}, {"query": "  ", "features": [" "]}, {"query": None}, {"features": "feature"},
                 {"query": "q", "features": [1]}, {"query": "q", "project_ids": []}, {"query": "q", "project_ids": "p1"},
                 {"query": "q", "project_ids": [None]}, {"query": "q", "kinds": []}, {"query": "q", "kinds": "experience"},
                 {"query": "q", "kinds": ["other"]}, {"query": "q", "include_shared": 1},
                 {"query": "q", "budget_chars": 4000}, {"context": {"pattern": ["x"]}}]
        cases += [{"query": "q", "limit": value} for value in (0, 21, True, 1.0, "5", None)]
        for candidate in cases:
            with self.subTest(candidate=type(candidate)):
                self.assertError(await self.memory.recall(candidate), "INVALID_ARGUMENT")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_search_original_content_partial_scores_and_nullable_provenance_passthrough(self):
        await self.init()
        rows = [item(score=None), item(kind="task_process", content_type="derived", task_id=None, source_id=None, score=-0.3)]
        expected = {"ok": True, "data": {"items": rows, "partial": True}}
        self.backend.replies["search"] = expected
        self.assertEqual(await self.memory.recall({"query": "q"}), expected)

    async def test_search_backend_error_never_becomes_empty_items(self):
        await self.init()
        for code in ("READ_FAILED", "UNAVAILABLE", "UNSUPPORTED", "TIMEOUT"):
            expected = failure(code, "known-source")
            self.backend.replies["search"] = expected
            self.assertEqual(await self.memory.recall({"query": "q", "project_ids": ["p1", "p2"]}), expected)

    async def test_response_source_pairs_and_kinds_checked_together(self):
        await self.init()
        for changes in ({"owner_id": "other"}, {"project_id": "p2"}, {"kind": "task_process"}, {"owner_id": ""}):
            self.backend.replies["search"] = {"ok": True, "data": {"items": [item(**changes)], "partial": False}}
            self.assertError(await self.memory.recall({"query": "q", "kinds": ["experience"]}), "ACCESS_DENIED")

    async def test_adapter_cannot_mutate_request_to_expand_response_authorization(self):
        await self.init()

        def mutate(request):
            request["sources"].append({"project_id": "p3", "owner_id": "other"})
            request["kinds"].append("task_process")
            return {"ok": True, "data": {"items": [item(project_id="p3", owner_id="other")], "partial": False}}

        self.backend.hooks["search"] = mutate
        self.assertError(await self.memory.recall({"query": "q", "kinds": ["experience"]}), "ACCESS_DENIED")

    async def test_malformed_search_results_are_errors_not_filtered_empty_success(self):
        await self.init()
        incomplete = item()
        del incomplete["source_id"]
        cases = [None, {}, {"ok": True, "data": {"items": [], "partial": 1}},
                 {"ok": True, "data": {"items": [item()] * 6, "partial": False}}]
        for row in (incomplete, item(content_type="derived"), item(content_type="unknown"), item(score=True),
                    item(score=float("nan")), item(content=None), item(task_id=12)):
            cases.append({"ok": True, "data": {"items": [row], "partial": False}})
        for value in cases:
            self.backend.replies["search"] = value
            with self.subTest(value=type(value)):
                self.assertError(await self.memory.recall({"query": "q"}), "BACKEND_ERROR")

    async def test_invalid_backend_status_envelope_and_shared_capability_rejected(self):
        for reply in ({"ok": True, "data": {"state": "ready", "message": "", "supports_shared": True}},
                      {"ok": True, "data": {"state": "unknown", "message": "", "supports_shared": False}},
                      {"ok": True, "data": {}, "error": {}}, {"ok": 1, "data": {}},
                      {"ok": False, "error": {"code": "UNAVAILABLE", "message": "missing source"}}):
            backend = FakeBackend()
            backend.replies["initialize"] = reply
            memory = NorthboundMemory(backend)
            self.assertError(await memory.initialize(config()), "BACKEND_ERROR")
            self.assertError(await memory.remember(record()), "NOT_READY")

    async def test_status_fault_passthrough_is_not_a_memory_processing_state(self):
        await self.init()
        expected = failure("UNAVAILABLE")
        self.backend.replies["status"] = expected
        self.assertEqual(await self.memory.status(), expected)
        self.backend.replies["status"] = ready("unavailable")
        self.assertEqual(await self.memory.status(), ready("unavailable"))

    async def test_timeout_arguments_rejected_without_backend_calls(self):
        await self.init()
        baseline = len(self.backend.calls)
        for timeout in (0, -1, True, None, "30", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(timeout=type(timeout)):
                for awaitable in (self.memory.initialize(config(), timeout=timeout), self.memory.status(timeout=timeout),
                                  self.memory.remember(record(), timeout=timeout), self.memory.recall({"query": "q"}, timeout=timeout)):
                    self.assertError(await awaitable, "INVALID_ARGUMENT")
        self.assertEqual(len(self.backend.calls), baseline)

    async def hang(self, _payload):
        await asyncio.Event().wait()

    async def test_read_timeout_and_initialization_timeout_are_finite_errors(self):
        self.backend.hooks["initialize"] = self.hang
        self.assertError(await self.memory.initialize(config(), timeout=0.01), "TIMEOUT")
        self.assertError(await self.memory.remember(record()), "NOT_READY")
        del self.backend.hooks["initialize"]
        await self.init()
        for name, call in (("search", lambda: self.memory.recall({"query": "q"}, timeout=0.01)),
                           ("status", lambda: self.memory.status(timeout=0.01))):
            self.backend.hooks[name] = self.hang
            self.assertError(await call(), "TIMEOUT")

    async def test_initialize_validation_consumes_deadline_before_backend_io(self):
        from kpbot_memory import northbound

        clock = [100.0]
        validate = northbound.validate_config

        def slow_validate(value):
            result = validate(value)
            clock[0] += 2.0
            return result

        with mock.patch.object(northbound, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(northbound, "validate_config", side_effect=slow_validate):
            self.assertError(await self.memory.initialize(config(), timeout=1.0), "TIMEOUT")
        self.assertEqual(self.backend.calls, [])

    async def test_initialize_checks_deadline_after_copying_backend_options(self):
        from kpbot_memory import northbound

        clock = [100.0]
        clone = northbound.clone_json
        copies = 0

        def slow_options_clone(value):
            nonlocal copies
            result = clone(value)
            copies += 1
            if copies == 2:  # Config copy, then backend options copy inside the lock.
                clock[0] += 2.0
            return result

        with mock.patch.object(northbound, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(northbound, "clone_json", side_effect=slow_options_clone):
            self.assertError(await self.memory.initialize(config(), timeout=1.0), "TIMEOUT")
        self.assertEqual(copies, 2)
        self.assertEqual(self.backend.calls, [])

    async def test_initialize_passes_only_budget_remaining_after_all_copies(self):
        from kpbot_memory import northbound

        clock = [100.0]
        validate, clone = northbound.validate_config, northbound.clone_json

        def validate_cost(value):
            result = validate(value)
            clock[0] += 0.4
            return result

        def clone_cost(value):
            result = clone(value)
            clock[0] += 0.1
            return result

        with mock.patch.object(northbound, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(northbound, "validate_config", side_effect=validate_cost), \
                mock.patch.object(northbound, "clone_json", side_effect=clone_cost):
            self.assertEqual(await self.memory.initialize(config(), timeout=1.0), ready())
        self.assertAlmostEqual(self.backend.calls[0][2], 0.4)

    async def test_data_payload_copy_expiry_does_not_start_write_or_search(self):
        from kpbot_memory import northbound

        await self.init()
        clone = northbound.clone_json
        for operation, payload in ((self.memory.remember, record()),
                                   (self.memory.remember, record("task_process")),
                                   (self.memory.recall, {"query": "original query"})):
            with self.subTest(operation=operation.__name__, payload=payload):
                clock = [100.0]

                def slow_clone(value):
                    result = clone(value)
                    clock[0] += 2.0
                    return result

                before = len(self.backend.calls)
                with mock.patch.object(northbound, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(northbound, "clone_json", side_effect=slow_clone):
                    self.assertError(await operation(payload, timeout=1.0), "TIMEOUT")
                self.assertEqual(len(self.backend.calls), before)

    async def test_data_methods_pass_only_remaining_budget_after_payload_copy(self):
        from kpbot_memory import northbound

        await self.init()
        clone = northbound.clone_json
        for operation, payload in ((self.memory.remember, record()),
                                   (self.memory.remember, record("task_process")),
                                   (self.memory.recall, {"query": "original query"})):
            with self.subTest(operation=operation.__name__, payload=payload):
                clock = [100.0]

                def clone_cost(value):
                    result = clone(value)
                    clock[0] += 0.25
                    return result

                with mock.patch.object(northbound, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(northbound, "clone_json", side_effect=clone_cost):
                    self.assertTrue((await operation(payload, timeout=1.0))["ok"])
                self.assertAlmostEqual(self.backend.calls[-1][2], 0.75)

    async def test_started_write_timeout_returns_write_unknown_and_does_not_retry(self):
        await self.init()
        self.backend.hooks["store_experience"] = self.hang
        self.assertError(await self.memory.remember(record(), timeout=0.01), "WRITE_UNKNOWN")
        self.assertEqual(sum(name == "store_experience" for name, _, _ in self.backend.calls), 1)

    async def test_started_write_cancellation_reports_uncertainty(self):
        await self.init()
        entered = asyncio.Event()

        async def hang(payload):
            entered.set()
            await asyncio.Event().wait()

        self.backend.hooks["store_process"] = hang
        task = asyncio.create_task(self.memory.remember(record("task_process")))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        self.assertError(await task, "WRITE_UNKNOWN")
        self.assertEqual(sum(name == "store_process" for name, _, _ in self.backend.calls), 1)

    async def test_adapter_cancel_locator_survives_outer_timeout_and_user_cancel(self):
        await self.init()
        for external in (False, True):
            entered = asyncio.Event()

            async def allocated_write(payload):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    exc.source_id = "viking://already-allocated-original"
                    raise

            self.backend.hooks["store_process"] = allocated_write
            before = len(self.backend.calls)
            if external:
                task = asyncio.create_task(self.memory.remember(record("task_process")))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                result = await task
            else:
                result = await self.memory.remember(record("task_process"), timeout=0.01)
            self.assertError(result, "WRITE_UNKNOWN")
            self.assertEqual(result["error"]["source_id"], "viking://already-allocated-original")
            self.assertEqual(len(self.backend.calls), before + 1)

    async def test_read_and_initialize_cancellation_propagate(self):
        for method in ("initialize", "search"):
            if method == "search":
                await self.init()
            entered = asyncio.Event()

            async def hang(payload):
                entered.set()
                await asyncio.Event().wait()

            self.backend.hooks[method] = hang
            awaitable = self.memory.initialize(config()) if method == "initialize" else self.memory.recall({"query": "q"})
            task = asyncio.create_task(awaitable)
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            del self.backend.hooks[method]

    async def test_exception_text_never_leaks_and_known_write_source_is_retained(self):
        await self.init()

        def fail(payload):
            exc = RuntimeError("TOP-SECRET and full task content")
            exc.source_id = "already-allocated-source"
            raise exc

        for name, awaitable, code in (("store_experience", self.memory.remember(record()), "WRITE_UNKNOWN"),
                                      ("search", self.memory.recall({"query": "q"}), "BACKEND_ERROR")):
            self.backend.hooks[name] = fail
            result = await awaitable
            self.assertError(result, code)
            self.assertNotIn("TOP-SECRET", str(result))
            self.assertEqual(result["error"]["source_id"], "already-allocated-source")

    async def test_data_calls_may_run_concurrently(self):
        await self.init()
        entered = 0
        both = asyncio.Event()

        async def concurrent(payload):
            nonlocal entered
            entered += 1
            if entered == 2:
                both.set()
            await both.wait()
            return {"ok": True, "data": {"items": [], "partial": False}}

        self.backend.hooks["search"] = concurrent
        results = await asyncio.wait_for(asyncio.gather(
            self.memory.recall({"query": "one"}), self.memory.recall({"query": "two"})), 1)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(entered, 2)

    async def test_close_failure_passthrough_then_retry_success(self):
        await self.init()
        expected = failure("BACKEND_ERROR")
        self.backend.replies["close"] = expected
        self.assertEqual(await self.memory.close(), expected)
        self.assertError(await self.memory.remember(record()), "NOT_READY")
        del self.backend.replies["close"]
        self.assertIsNone(await self.memory.close())
        self.assertEqual(await self.memory.status(), ready("closed"))

    async def test_openviking_close_failure_status_then_retry_releases_client(self):
        from tests.memory.test_openviking_backend import FakeClient, OpenVikingBackend

        client = FakeClient()
        memory = NorthboundMemory(OpenVikingBackend(client=client))
        settings = config()
        settings["options"] = {"api_key": "test-only-placeholder", "account_id": "account-1",
                               "user_id": "alice", "owner_id": settings["user_id"]}
        self.assertTrue((await memory.initialize(settings))["ok"])
        release = client.aclose

        async def failed_release():
            raise RuntimeError("DO NOT EXPOSE")

        client.aclose = failed_release
        self.assertError(await memory.close(), "BACKEND_ERROR")
        self.assertEqual((await memory.status())["data"]["state"], "closed")
        client.aclose = release
        self.assertIsNone(await memory.close())
        self.assertTrue(client.closed)
        self.assertEqual(client.close_count, 1)
        self.assertIsNone(await memory.close())
        self.assertEqual(client.close_count, 1)

    async def test_cancelled_close_status_closed_does_not_prevent_retry(self):
        await self.init()
        entered = asyncio.Event()

        async def interrupted_close(payload):
            self.backend.state = "closed"
            entered.set()
            await asyncio.Event().wait()

        self.backend.hooks["close"] = interrupted_close
        task = asyncio.create_task(self.memory.close())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.memory.status(), ready("closed"))
        del self.backend.hooks["close"]
        self.assertIsNone(await self.memory.close())
        self.assertEqual(sum(name == "close" for name, _, _ in self.backend.calls), 2)

    async def test_late_status_does_not_reopen_completed_close(self):
        await self.init()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_status(payload):
            entered.set()
            await release.wait()
            return ready("unavailable")

        self.backend.hooks["status"] = delayed_status
        task = asyncio.create_task(self.memory.status())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIsNone(await self.memory.close())
        release.set()
        self.assertEqual(await task, ready("closed"))
        self.assertError(await self.memory.initialize(config()), "NOT_READY")
        self.assertIsNone(await self.memory.close())
        self.assertEqual(sum(name == "close" for name, _, _ in self.backend.calls), 1)

    async def test_close_success_must_be_null(self):
        self.backend.replies["close"] = {"ok": True, "data": {}}
        self.assertError(await self.memory.close(), "BACKEND_ERROR")

    async def test_timeout_before_write_execution_does_not_claim_unknown_save(self):
        await self.init()
        result = await self.memory.remember(record(), timeout=1e-300)
        self.assertError(result, "TIMEOUT")
        self.assertEqual([name for name, _, _ in self.backend.calls], ["initialize"])

    async def test_initialization_lock_wait_counts_toward_total_timeout(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def initialize(payload):
            entered.set()
            await release.wait()
            return ready()

        self.backend.hooks["initialize"] = initialize
        first = asyncio.create_task(self.memory.initialize(config()))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertError(await self.memory.initialize(config(), timeout=0.01), "TIMEOUT")
        release.set()
        self.assertEqual(await first, ready())
        self.assertEqual(sum(name == "initialize" for name, _, _ in self.backend.calls), 1)

    async def test_concurrent_writes_remain_independent(self):
        await self.init()
        count = 0
        both = asyncio.Event()

        async def store(payload):
            nonlocal count
            count += 1
            if count == 2:
                both.set()
            await both.wait()
            return {"ok": True, "data": {"source_id": payload["task_id"], "processing": "pending"}}

        self.backend.hooks["store_experience"] = store
        results = await asyncio.wait_for(asyncio.gather(
            self.memory.remember({**record(), "task_id": "t1"}),
            self.memory.remember({**record(), "task_id": "t2"})), 1)
        self.assertEqual([result["data"]["source_id"] for result in results], ["t1", "t2"])

    async def test_invalid_known_source_unicode_is_not_emitted(self):
        await self.init()
        self.backend.replies["store_experience"] = {"ok": True, "data": {"source_id": "\ud800"}}
        result = await self.memory.remember(record())
        self.assertError(result, "WRITE_UNKNOWN")
        self.assertIsNone(result["error"]["source_id"])

    async def test_close_failure_before_initialize_reports_unavailable_not_uninitialized(self):
        self.backend.replies["close"] = failure("BACKEND_ERROR")
        self.assertEqual(await self.memory.close(), failure("BACKEND_ERROR"))
        self.assertEqual((await self.memory.status())["data"]["state"], "unavailable")

    async def test_close_cancellation_propagates_and_blocks_data_until_reinitialized(self):
        await self.init()
        entered = asyncio.Event()

        async def close(payload):
            entered.set()
            await asyncio.Event().wait()

        self.backend.hooks["close"] = close
        task = asyncio.create_task(self.memory.close())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertError(await self.memory.remember(record()), "NOT_READY")
        del self.backend.hooks["close"]
        self.assertIsNone(await self.memory.close())

    async def test_missing_backend_methods_fail_before_resources_are_used(self):
        with self.assertRaises(TypeError):
            NorthboundMemory(object())


if __name__ == "__main__":
    unittest.main()
