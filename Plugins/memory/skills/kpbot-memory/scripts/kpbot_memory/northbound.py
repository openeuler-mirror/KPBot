"""Five asynchronous northbound operations over an injected memory adapter.

NorthboundMemory(backend) accepts the six async methods in adapter.md. It does
not construct an SDK client, start a service, or infer a login. initialize
receives trusted configuration supplied by the
application (normally backend_config.load_config), never a business request.
Each instance has an in-process lifetime; CLI invocations cannot share it.

The facade only validates identity, scope and JSON shapes. It never summarises,
extracts experience, truncates content, retries writes or invents provenance.
Read cancellation propagates; cancellation after entering a write is reported
as WRITE_UNKNOWN because it cannot prove that the adapter did not persist it.
"""
from __future__ import annotations

import asyncio
import math
from time import monotonic
from typing import Any, Callable

from .backend_config import BackendConfigError, clone_json, nonempty_string, validate_config

__all__ = ["NorthboundMemory"]

KINDS = ("experience", "task_process")
STATES = ("uninitialized", "ready", "unavailable", "closed")
PROCESSING = ("ready", "pending", "failed", "unknown")


def _error(code: str, message: str, source_id: str | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "source_id": source_id}}


def _status(state: str, message: str = "") -> dict:
    return {"ok": True, "data": {"state": state, "message": message, "supports_shared": False}}


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendConfigError("timeout must be a finite positive number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise BackendConfigError("timeout must be a finite positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise BackendConfigError("timeout must be a finite positive number")
    return result


def _known_source(value: Any) -> str | None:
    if nonempty_string(value):
        try:
            value.encode("utf-8")
            return value
        except UnicodeError:
            pass
    return None


def _exception_source(exc: BaseException) -> str | None:
    """wait_for chains TimeoutError from the adapter's CancelledError."""
    visited = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        source = _known_source(getattr(current, "source_id", None))
        if source is not None:
            return source
        current = current.__cause__
    return None


def _source(result: Any) -> str | None:
    """Retain only a source the backend actually supplied, never synthesize one."""
    if isinstance(result, dict):
        for field in ("data", "error"):
            part = result.get(field)
            if isinstance(part, dict):
                source = _known_source(part.get("source_id"))
                if source is not None:
                    return source
    return None


def _envelope(result: Any) -> dict:
    result = clone_json(result)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise BackendConfigError("backend returned an invalid result envelope", "BACKEND_ERROR")
    if result["ok"]:
        if set(result) != {"ok", "data"} or not isinstance(result["data"], dict):
            raise BackendConfigError("backend returned an invalid success envelope", "BACKEND_ERROR")
    else:
        error = result.get("error")
        if (
            set(result) != {"ok", "error"}
            or not isinstance(error, dict)
            or set(error) != {"code", "message", "source_id"}
            or not nonempty_string(error["code"])
            or not isinstance(error["message"], str)
            or (error["source_id"] is not None and not nonempty_string(error["source_id"]))
        ):
            raise BackendConfigError("backend returned an invalid error envelope", "BACKEND_ERROR")
    return result


def _check_status(data: dict) -> None:
    if (
        data.get("state") not in STATES
        or not isinstance(data.get("message"), str)
        or data.get("supports_shared") is not False
    ):
        raise BackendConfigError("backend returned an invalid private-only status", "BACKEND_ERROR")


class NorthboundMemory:
    """Application-owned facade for a single trusted local user's configuration."""

    def __init__(self, backend: Any) -> None:
        for method in ("initialize", "status", "store_experience", "store_process", "search", "close"):
            if not callable(getattr(backend, method, None)):
                raise TypeError("backend must provide all six adapter methods")
        self._backend = backend
        self._config: dict | None = None
        self._initialized = False
        self._state = "uninitialized"
        self._message = ""
        self._lifecycle = asyncio.Lock()
        self._close_complete = False

    async def _bounded(self, action: Callable, deadline: float, *, write: bool = False) -> Any:
        # The caller establishes this deadline before validation/copying. CPU
        # work is not preempted, but an expired budget must not start backend IO.
        timeout = deadline - monotonic()
        if timeout <= 0:
            return _error("TIMEOUT", "operation timed out before execution")
        started = False
        cancelled_source = None

        async def run() -> Any:
            nonlocal started, cancelled_source
            # No await separates this marker from entering a write adapter call.
            started = True
            remaining = max(deadline - monotonic(), 0.0)
            if remaining <= 0:
                started = False
                return _error("TIMEOUT", "operation timed out before execution")
            try:
                return await action(remaining)
            except asyncio.CancelledError as exc:
                # External cancellation may create a *different* outer exception
                # from the child task's cancellation. Retain its confirmed locator.
                cancelled_source = _exception_source(exc)
                raise

        try:
            return await asyncio.wait_for(run(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            source_id = _exception_source(exc) or cancelled_source
            if write and started:
                return _error("WRITE_UNKNOWN", "write timed out; persistence is unconfirmed", source_id)
            return _error("TIMEOUT", "operation timed out", source_id)
        except asyncio.CancelledError as exc:
            if write and started:
                source_id = _exception_source(exc) or cancelled_source
                return _error("WRITE_UNKNOWN", "write was cancelled; persistence is unconfirmed", source_id)
            raise
        except Exception as exc:
            source_id = _exception_source(exc) or cancelled_source
            # Exceptions violate the adapter's ordinary-result contract. Do not
            # expose arbitrary exception text (it may contain credentials/body).
            if write and started:
                return _error("WRITE_UNKNOWN", "backend write failed without a confirmed result", source_id)
            return _error("BACKEND_ERROR", "backend call failed without a valid result", source_id)

    async def initialize(self, config: dict, *, timeout: float = 30.0) -> dict:
        entered_at = monotonic()
        try:
            timeout = _timeout(timeout)
            deadline = entered_at + timeout
            config = validate_config(config)
            owner = config["options"].get("owner_id", config["user_id"])
            if owner != config["user_id"]:
                raise BackendConfigError("backend owner differs from the trusted user", "ACCESS_DENIED")
            # Backend provider user_id may differ; KPBot ownership never does.
            config["options"]["owner_id"] = config["user_id"]
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)

        async def action(remaining: float) -> dict:
            async with self._lifecycle:
                if self._close_complete or self._state == "closed":
                    return _error("NOT_READY", "closed instances cannot be reinitialized")
                if self._config is not None and config != self._config:
                    return _error("INVALID_ARGUMENT", "configuration changes require a new instance")
                self._config = clone_json(config)
                self._initialized = False
                self._state = "unavailable"
                self._message = "initialization has not completed"
                options = clone_json(config["options"])
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return _error("TIMEOUT", "initialization timed out before execution")
                raw = await self._backend.initialize(options, timeout=remaining)
                result = _envelope(raw)
                if result["ok"]:
                    _check_status(result["data"])
                    self._state = result["data"]["state"]
                    self._message = result["data"]["message"]
                    self._initialized = self._state == "ready"
                else:
                    self._message = result["error"]["message"]
                return result

        result = await self._bounded(action, deadline)
        if not result["ok"] and self._state != "closed" and not self._initialized:
            self._state = "unavailable"
            self._message = result["error"]["message"]
        return result

    async def status(self, *, timeout: float = 30.0) -> dict:
        entered_at = monotonic()
        try:
            timeout = _timeout(timeout)
            deadline = entered_at + timeout
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)
        if self._close_complete or self._state == "closed":
            return _status("closed")
        if self._config is None:
            return _status(self._state, self._message)

        async def action(remaining: float) -> dict:
            result = _envelope(await self._backend.status(timeout=remaining))
            if self._close_complete:
                return _status("closed")
            if result["ok"]:
                _check_status(result["data"])
                # A backend claiming ready cannot authenticate/initialize a
                # facade whose own initialize operation has never succeeded.
                if not self._initialized and result["data"]["state"] == "ready":
                    return _status(self._state, self._message)
                self._state = result["data"]["state"]
                self._message = result["data"]["message"]
            return result

        return await self._bounded(action, deadline)

    def _ready(self) -> dict | None:
        if not self._initialized or self._state == "closed":
            return _error("NOT_READY", "memory has not been successfully initialized or is closed")
        return None

    async def remember(self, record: dict, *, timeout: float = 30.0) -> dict:
        entered_at = monotonic()
        try:
            timeout = _timeout(timeout)
            deadline = entered_at + timeout
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)
        not_ready = self._ready()
        if not_ready is not None:
            return not_ready
        assert self._config is not None
        try:
            if not isinstance(record, dict):
                raise BackendConfigError("record must be an object")
            if set(record) - {"kind", "project_id", "owner_id", "task_id", "visibility", "content"}:
                raise BackendConfigError("record contains unsupported fields")
            if record.get("kind") not in KINDS:
                raise BackendConfigError("record kind must be experience or task_process")
            for field in ("project_id", "task_id"):
                if not nonempty_string(record.get(field)):
                    raise BackendConfigError(f"record {field} must be a non-empty string")
            owner_id = record.get("owner_id", self._config["user_id"])
            if not nonempty_string(owner_id):
                raise BackendConfigError("owner_id must be a non-empty string")
            if owner_id != self._config["user_id"]:
                raise BackendConfigError("record owner differs from the trusted user", "ACCESS_DENIED")
            if record["project_id"] not in self._config["allowed_projects"]:
                raise BackendConfigError("record project is outside the configured scope", "ACCESS_DENIED")
            if record.get("visibility", "private") != "private":
                raise BackendConfigError("shared memory is not supported", "UNSUPPORTED")
            if not isinstance(record.get("content"), (str, dict, list)):
                raise BackendConfigError("content must be text, an object or an array")
            prepared = clone_json(record)
            prepared["owner_id"] = owner_id
            prepared["visibility"] = "private"
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)

        async def action(remaining: float) -> dict:
            method = self._backend.store_experience if prepared["kind"] == "experience" else self._backend.store_process
            raw = await method(prepared, timeout=remaining)
            try:
                result = _envelope(raw)
                if result["ok"] and (
                    not nonempty_string(result["data"].get("source_id"))
                    or result["data"].get("processing") not in PROCESSING
                ):
                    raise BackendConfigError("backend returned an invalid storage result")
                return result
            except BackendConfigError:
                return _error("WRITE_UNKNOWN", "backend did not confirm a complete storage result", _source(raw))

        return await self._bounded(action, deadline, write=True)

    async def recall(self, request: dict, *, timeout: float = 30.0) -> dict:
        entered_at = monotonic()
        try:
            timeout = _timeout(timeout)
            deadline = entered_at + timeout
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)
        not_ready = self._ready()
        if not_ready is not None:
            return not_ready
        assert self._config is not None
        try:
            if not isinstance(request, dict):
                raise BackendConfigError("recall request must be an object")
            if set(request) & {"owner_id", "sources", "user_id"}:
                raise BackendConfigError("query identity and sources are controlled by trusted configuration", "ACCESS_DENIED")
            if set(request) - {"query", "features", "project_ids", "include_shared", "kinds", "limit"}:
                raise BackendConfigError("recall request contains unsupported fields")
            shared = request.get("include_shared", False)
            if type(shared) is not bool:
                raise BackendConfigError("include_shared must be a boolean")
            if shared:
                raise BackendConfigError("shared memory is not supported", "UNSUPPORTED")
            query = request.get("query", "")
            features = request.get("features", [])
            if not isinstance(query, str) or not isinstance(features, list) or any(not isinstance(feature, str) for feature in features):
                raise BackendConfigError("query must be text and features a string array")
            if not query.strip() and not any(feature.strip() for feature in features):
                raise BackendConfigError("query or features must provide non-empty search information")
            projects = request.get("project_ids", [self._config["current_project"]])
            if not isinstance(projects, list) or not projects or any(not nonempty_string(project) for project in projects):
                raise BackendConfigError("project_ids must be a non-empty array of project IDs")
            if any(project not in self._config["allowed_projects"] for project in projects):
                raise BackendConfigError("query project is outside the configured scope", "ACCESS_DENIED")
            kinds = request.get("kinds", list(KINDS))
            if not isinstance(kinds, list) or not kinds or any(kind not in KINDS for kind in kinds):
                raise BackendConfigError("kinds must be a non-empty subset of supported kinds")
            limit = request.get("limit", 5)
            if type(limit) is not int or not 1 <= limit <= 20:
                raise BackendConfigError("limit must be an integer from 1 to 20")
            prepared = clone_json({
                "query": query,
                "features": features,
                "sources": [{"project_id": project, "owner_id": self._config["user_id"]} for project in dict.fromkeys(projects)],
                "kinds": list(dict.fromkeys(kinds)),
                "limit": limit,
            })
        except BackendConfigError as exc:
            return _error(exc.code, exc.message)

        # Freeze authorization independently of the adapter's mutable argument.
        allowed_sources = frozenset((source["project_id"], source["owner_id"]) for source in prepared["sources"])
        allowed_kinds = tuple(prepared["kinds"])

        async def action(remaining: float) -> dict:
            raw = await self._backend.search(prepared, timeout=remaining)
            try:
                result = _envelope(raw)
                if not result["ok"]:
                    return result
                data = result["data"]
                items = data.get("items")
                if not isinstance(items, list) or len(items) > limit or type(data.get("partial")) is not bool:
                    raise BackendConfigError("backend returned an invalid search result", "BACKEND_ERROR")
                for item in items:
                    self._check_item(item, allowed_sources, allowed_kinds)
                return result
            except BackendConfigError as exc:
                return _error(exc.code if exc.code == "ACCESS_DENIED" else "BACKEND_ERROR", exc.message)

        return await self._bounded(action, deadline)

    @staticmethod
    def _check_item(item: Any, sources: set, kinds: list) -> None:
        required = {"content", "content_type", "kind", "project_id", "owner_id", "task_id", "source_id", "score"}
        if not isinstance(item, dict) or not required <= set(item):
            raise BackendConfigError("backend returned incomplete search provenance", "BACKEND_ERROR")
        if (
            not nonempty_string(item["project_id"])
            or not nonempty_string(item["owner_id"])
            or not isinstance(item["kind"], str)
            or (item["project_id"], item["owner_id"]) not in sources
            or item["kind"] not in kinds
        ):
            raise BackendConfigError("backend returned content outside the authorized query scope", "ACCESS_DENIED")
        if (
            not isinstance(item["content"], (str, dict, list))
            or item["content_type"] not in ("original", "excerpt", "derived")
            or (item["kind"] == "experience" and item["content_type"] != "original")
            or any(item[field] is not None and not nonempty_string(item[field]) for field in ("task_id", "source_id"))
            or (item["score"] is not None and (isinstance(item["score"], bool) or not isinstance(item["score"], (int, float))))
        ):
            raise BackendConfigError("backend returned invalid search content or provenance", "BACKEND_ERROR")

    async def close(self) -> None | dict:
        """Release this instance only; never remove data or shut down a service."""
        async with self._lifecycle:
            # A status probe may report closed even when backend close failed or
            # was cancelled before releasing resources. Only null confirms it.
            if self._close_complete:
                return None
            self._initialized = False
            self._state = "unavailable"
            self._message = "close has not completed"
            try:
                result = await self._backend.close()
                if result is not None:
                    result = _envelope(result)
                    if result["ok"]:
                        return _error("BACKEND_ERROR", "backend close success must be null")
                    return result
            except asyncio.CancelledError:
                raise
            except Exception:
                return _error("BACKEND_ERROR", "backend close failed without a valid result")
            self._close_complete = True
            self._state = "closed"
            self._message = ""
            return None
