"""TencentDB Agent Memory (MemoryCore) HTTP adapter for the six-method memory
backend contract.

Requires httpx only at initialize(), never reads SDK/default configuration, and
never starts a server. Pass ``client_factory(**httpx_client_kwargs)`` or an
already configured ``client`` for testing; clients expose async request/aclose.
Injected clients must honour the supplied timeout and must not follow redirects.

Options: endpoint (HTTP(S) origin, default http://127.0.0.1:8420), api_key
(Bearer token, default "local" for gateways without server.apiKey), service_id
(required x-tdai-service-id instance identity), agent_id (v3 agent isolation
dimension, default "kpbot"), owner_id (KPBot identity injected by the common
layer, mapped to the v3 user_id), max_input_bytes (default 2097152 UTF-8 bytes
of the complete submission envelope), and chunk_chars (payload characters per
L0 message, 512..8000, default 8000).

Field mapping: project_id -> team_id, owner_id -> user_id, task_id -> task_id,
agent_id fixed per configuration. Each submission is one fresh v3 conversation
session named by its source_id; the complete reversible JSON envelope is split
into chunk messages tagged with a parseable header, and persistence is only
confirmed after a readback reassembles and byte-compares the whole session.
Native L1 extraction runs asynchronously and has no per-record progress
endpoint, so processing is always "unknown". Search reuses the gateway keyword
conversation search and returns the verified complete original (content_type
"original") for both kinds. No local content store, operation journal, or
write retry is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

_KINDS = frozenset({"experience", "task_process"})
_FIELDS = ("kind", "project_id", "owner_id", "task_id", "visibility", "content")
_HEADER = re.compile(
    r"^\[\[kpbot-memory:1:([0-9a-z][0-9a-z-]{0,127}):(experience|task_process):(\d+):(\d+)\]\]\n"
)
_HEALTH_TEAM = "kpbot-health-probe"
_NATIVE_QUERY_MAX = 2048
_SEARCH_LIMIT = 100
_MESSAGE_MAX = 8192
_BATCH_MESSAGES = 100
_BATCH_BYTES = 700_000
_DEFINITIVE = frozenset({"INVALID_ARGUMENT", "TOO_LARGE", "ACCESS_DENIED", "UNSUPPORTED"})


class _Failure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _success(data: Any) -> dict:
    return {"ok": True, "data": data}


def _error(code: str, message: str, source_id: str | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "source_id": source_id}}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_value(value: Any) -> None:
    """Reject coercions (tuple, non-string keys, NaN) before reversible encoding."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise _Failure("INVALID_ARGUMENT", "Input must contain only finite JSON-compatible values.")


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON constant")


class SouthboundMemory:
    """Async adapter; one configured owner, any number of projects, private only."""

    def __init__(self, *, client_factory: Callable | None = None, client: Any = None):
        if client_factory is not None and client is not None:
            raise ValueError("Provide either client_factory or client, not both")
        self._factory = client_factory
        self._provided_client = client
        self._client = None
        self._options = None
        self._initialized = False
        self._state = "uninitialized"
        self._message = "Not initialized."
        self._lifecycle = asyncio.Lock()
        self._active_data = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _status_data(self) -> dict:
        return {"state": self._state, "message": self._message, "supports_shared": False}

    @staticmethod
    def _validate_timeout(timeout: Any) -> float:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise _Failure("INVALID_ARGUMENT", "timeout must be a finite number greater than zero.")
        return float(timeout)

    async def _bounded(self, operation: Callable, timeout: Any, *, source_id=None,
                       write_started: list | None = None) -> dict:
        try:
            seconds = self._validate_timeout(timeout)
            deadline = time.monotonic() + seconds
            return await asyncio.wait_for(operation(deadline), timeout=seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            uncertain = write_started is not None and write_started[0]
            return _error("WRITE_UNKNOWN" if uncertain else "TIMEOUT",
                          "Write result is uncertain after timeout." if uncertain
                          else "Operation exceeded its total waiting deadline.",
                          source_id if uncertain else None)
        except _Failure as exc:
            return _error(exc.code, exc.message, source_id)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            uncertain = write_started is not None and write_started[0]
            return _error("WRITE_UNKNOWN" if uncertain else "INVALID_ARGUMENT",
                          "Write result could not be confirmed." if uncertain else
                          "Input cannot be represented as supported JSON.", source_id if uncertain else None)
        except Exception:
            # Do not expose native messages, response bodies, headers or credentials.
            uncertain = write_started is not None and write_started[0]
            return _error("WRITE_UNKNOWN" if uncertain else "BACKEND_ERROR",
                          "Write result could not be confirmed." if uncertain else
                          "Unexpected backend operation failure.", source_id if uncertain else None)

    async def _data_bounded(self, operation: Callable, timeout: Any, **kwargs) -> dict:
        self._active_data += 1
        self._idle.clear()
        try:
            return await self._bounded(operation, timeout, **kwargs)
        finally:
            self._active_data -= 1
            if not self._active_data:
                self._idle.set()

    @staticmethod
    def _control_free(value: str) -> bool:
        return not any(ord(char) < 32 or ord(char) == 127 for char in value)

    @staticmethod
    def _normalize_options(options: Any) -> dict:
        if not isinstance(options, dict):
            raise _Failure("INVALID_ARGUMENT", "options must be an object.")
        allowed = {"endpoint", "api_key", "service_id", "agent_id", "owner_id",
                   "max_input_bytes", "chunk_chars"}
        if set(options) - allowed:
            raise _Failure("INVALID_ARGUMENT", "Unknown TencentDB configuration option.")
        normalized = dict(options)
        normalized.setdefault("endpoint", "http://127.0.0.1:8420")
        normalized.setdefault("api_key", "local")
        normalized.setdefault("agent_id", "kpbot")
        normalized.setdefault("max_input_bytes", 2_097_152)
        normalized.setdefault("chunk_chars", 8000)
        for name in ("api_key", "service_id", "agent_id", "owner_id"):
            value = normalized.get(name)
            if not _text(value) or len(value) > 128 or not SouthboundMemory._control_free(value):
                raise _Failure("INVALID_ARGUMENT", f"{name} must be a nonempty header-safe string.")
        endpoint = normalized["endpoint"]
        if not isinstance(endpoint, str):
            raise _Failure("INVALID_ARGUMENT", "endpoint must be an HTTP(S) origin URL.")
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            host = None
        if (host is None or any(ord(char) <= 32 or ord(char) == 127 for char in host)
                or parsed.scheme not in ("http", "https") or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise _Failure("INVALID_ARGUMENT",
                           "endpoint must be a credential-free HTTP(S) origin (host[:port] only).")
        normalized["endpoint"] = endpoint.rstrip("/")
        size = normalized["max_input_bytes"]
        if type(size) is not int or not 0 < size <= 33_554_432:
            raise _Failure("INVALID_ARGUMENT", "max_input_bytes must be a positive integer up to 33554432.")
        chunk = normalized["chunk_chars"]
        if type(chunk) is not int or not 512 <= chunk <= 8000:
            raise _Failure("INVALID_ARGUMENT", "chunk_chars must be an integer from 512 through 8000.")
        return normalized

    async def initialize(self, options: dict, *, timeout=30.0) -> dict:
        async def operation(deadline):
            config = self._normalize_options(options)
            async with self._lifecycle:
                if self._state == "closed":
                    raise _Failure("NOT_READY", "Closed adapters cannot be initialized again.")
                if self._initialized:
                    if config != self._options:
                        raise _Failure("INVALID_ARGUMENT", "Use a new instance to change configuration.")
                    notice = await self._probe(config, deadline)
                else:
                    if self._client is not None and self._client is not self._provided_client:
                        await self._client.aclose()
                    if self._provided_client is not None:
                        self._client = self._provided_client
                    else:
                        factory = self._factory
                        if factory is None:
                            try:
                                import httpx
                            except ImportError:
                                raise _Failure("UNAVAILABLE", "httpx is required to initialize this backend.")
                            factory = httpx.AsyncClient
                        self._client = factory(base_url=config["endpoint"],
                                               headers={"Authorization": "Bearer " + config["api_key"],
                                                        "x-tdai-service-id": config["service_id"]},
                                               timeout=self._remaining(deadline),
                                               follow_redirects=False, trust_env=False)
                    notice = await self._probe(config, deadline)
                    self._options = config
                    self._initialized = True
                self._state = "ready"
                self._message = notice
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._state != "closed" and
                result["error"]["code"] not in ("INVALID_ARGUMENT", "NOT_READY")):
            self._state = "unavailable"
            self._message = result["error"]["message"]
        return result

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    @staticmethod
    def _map_code(code: Any) -> str:
        if type(code) is not int:
            return "BACKEND_ERROR"
        if code in (401, 403):
            return "ACCESS_DENIED"
        if code in (400, 422):
            return "INVALID_ARGUMENT"
        if code == 413:
            return "TOO_LARGE"
        if code in (404, 503):
            return "UNAVAILABLE"
        return "BACKEND_ERROR"

    async def _request(self, method: str, path: str, deadline: float, *,
                       write_started: list | None = None, **kwargs):
        remaining = self._remaining(deadline)
        if write_started is not None:
            write_started[0] = True
        try:
            response = await self._client.request(method, path, timeout=remaining, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
                raise asyncio.TimeoutError from None
            raise _Failure("UNAVAILABLE", "MemoryCore HTTP connection failed.") from None
        status = response.status_code
        if status == 504:
            raise asyncio.TimeoutError
        if status in (401, 403):
            raise _Failure("ACCESS_DENIED", "MemoryCore authentication or service identity was denied.")
        if 300 <= status < 400:
            raise _Failure("BACKEND_ERROR", "MemoryCore redirects are not followed.")
        try:
            data = response.json()
        except Exception:
            raise _Failure("BACKEND_ERROR", "MemoryCore returned an invalid JSON response.") from None
        if not isinstance(data, dict):
            raise _Failure("BACKEND_ERROR", "MemoryCore response must be an object.")
        if not 200 <= status < 300:
            code = data.get("code")
            if status == 404 and type(code) is not int:
                raise _Failure("UNAVAILABLE", "MemoryCore gateway does not expose the required v3 API.")
            raise _Failure(self._map_code(code) if type(code) is int else self._map_code(status),
                           f"MemoryCore request failed (HTTP {status}, code {code}).")
        return data

    async def _v3(self, method: str, path: str, deadline: float, *, body: dict, **kwargs) -> Any:
        payload = await self._request(method, path, deadline, json=body, **kwargs)
        code = payload.get("code")
        if type(code) is not int:
            raise _Failure("BACKEND_ERROR", "MemoryCore response lacks a valid envelope code.")
        if code != 0:
            raise _Failure(self._map_code(code), f"MemoryCore request failed (code {code}).")
        return payload.get("data")

    async def _probe(self, config: dict, deadline: float) -> str:
        health = await self._request("GET", "/health", deadline)
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise _Failure("UNAVAILABLE", "MemoryCore health check did not report ok.")
        stores = health.get("stores")
        stores = stores if isinstance(stores, dict) else {}
        fts = stores.get("ftsAvailable") is True
        vectors = stores.get("embeddingService") is True
        if not fts and not vectors:
            raise _Failure("UNAVAILABLE",
                           "MemoryCore has neither BM25 (FTS5) nor an embedding provider; search cannot return results.")
        data = await self._v3("POST", "/v3/conversation/count", deadline, body={
            "team_id": _HEALTH_TEAM, "agent_id": config["agent_id"], "user_id": config["owner_id"]})
        if not isinstance(data, dict) or type(data.get("total")) is not int:
            raise _Failure("BACKEND_ERROR", "MemoryCore count probe returned an invalid result.")
        search_mode = "BM25 keyword search" if fts else "vector embedding search"
        return (f"MemoryCore gateway reachable; L0 storage and {search_mode} verified. "
                "LLM memory extraction progress and per-record index state are not probed.")

    async def status(self, *, timeout=30.0) -> dict:
        async def operation(deadline):
            async with self._lifecycle:
                if self._initialized and self._state != "closed":
                    try:
                        notice = await self._probe(self._options, deadline)
                    except _Failure as exc:
                        self._state, self._message = "unavailable", exc.message
                    else:
                        self._state, self._message = "ready", notice
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._initialized and self._state != "closed" and
                result["error"]["code"] != "INVALID_ARGUMENT"):
            self._state, self._message = "unavailable", result["error"]["message"]
        return result

    def _require_ready(self):
        if not self._initialized or self._state == "closed":
            raise _Failure("NOT_READY", "Backend is not initialized or has been closed.")

    def _validate_record(self, record: Any, kind: str) -> None:
        if not isinstance(record, dict) or any(name not in record for name in _FIELDS):
            raise _Failure("INVALID_ARGUMENT", "All six record fields are required.")
        if record["kind"] != kind:
            raise _Failure("INVALID_ARGUMENT", "Record kind does not match the storage method.")
        for field in ("project_id", "owner_id", "task_id"):
            if not _text(record[field]):
                raise _Failure("INVALID_ARGUMENT", f"{field} must be a nonempty string.")
        if record["visibility"] != "private":
            raise _Failure("UNSUPPORTED", "Only private records are supported.")
        if record["owner_id"] != self._options["owner_id"]:
            raise _Failure("ACCESS_DENIED", "Record owner is outside the authenticated binding.")
        if type(record["content"]) not in (str, dict, list):
            raise _Failure("INVALID_ARGUMENT", "content must be text, a JSON object, or an array.")
        _json_value(record)

    def _chunk_messages(self, text: str, kind: str, source_id: str) -> list[dict]:
        size = self._options["chunk_chars"]
        parts = [text[index:index + size] for index in range(0, len(text), size)]
        messages = []
        for index, part in enumerate(parts):
            content = f"[[kpbot-memory:1:{source_id}:{kind}:{index}:{len(parts)}]]\n{part}"
            if len(content) > _MESSAGE_MAX:
                raise _Failure("BACKEND_ERROR", "Chunk encoding exceeded the message size limit.")
            messages.append({"role": "user", "content": content})
        return messages

    @staticmethod
    def _batches(messages: list[dict]) -> list[list[dict]]:
        # v3 conversation/add accepts at most 100 messages and the gateway body
        # limit is 1 MiB; 700k UTF-8 bytes of content leaves a safety margin.
        batches, batch, size = [], [], 96
        for message in messages:
            cost = len(message["content"].encode("utf-8")) + 48
            if batch and (len(batch) >= _BATCH_MESSAGES or size + cost > _BATCH_BYTES):
                batches.append(batch)
                batch, size = [], 96
            batch.append(message)
            size += cost
        if batch:
            batches.append(batch)
        return batches

    async def store_experience(self, record: dict, *, timeout=30.0) -> dict:
        return await self._store(record, "experience", timeout)

    async def store_process(self, record: dict, *, timeout=30.0) -> dict:
        return await self._store(record, "task_process", timeout)

    async def _store(self, record: dict, kind: str, timeout: Any) -> dict:
        started = [False]
        locator = [None]
        async def operation(deadline):
            self._require_ready()
            self._validate_record(record, kind)
            frozen = json.loads(_encode(record))
            source_id = f"kpbot-mem-{uuid.uuid4().hex}"
            locator[0] = source_id
            envelope = {"schema_version": 1, "source_id": source_id, "record": frozen,
                        "record_sha256": hashlib.sha256(_encode(frozen)).hexdigest()}
            body = _encode(envelope)
            maximum = self._options["max_input_bytes"]
            if len(body) > maximum:
                raise _Failure("TOO_LARGE", f"Complete submission envelope exceeds {maximum} UTF-8 bytes.")
            messages = self._chunk_messages(body.decode("utf-8"), kind, source_id)
            failure = None
            for batch in self._batches(messages):
                if failure is not None:
                    # Never issue new writes once a batch reported a failure.
                    break
                try:
                    await self._v3("POST", "/v3/conversation/add", deadline,
                                   write_started=started,
                                   body={"session_id": source_id, "messages": batch,
                                         "team_id": frozen["project_id"],
                                         "agent_id": self._options["agent_id"],
                                         "user_id": self._options["owner_id"],
                                         "task_id": frozen["task_id"]})
                except _Failure as exc:
                    # A native HTTP/error code carries no execution-stage proof.
                    # Even 400/413/403 may follow a partially executed write.
                    failure = exc
                except asyncio.TimeoutError:
                    failure = _Failure("TIMEOUT", "Native write result is uncertain.")
            try:
                text, present, expected = await self._reassemble(frozen["project_id"], source_id, deadline)
            except asyncio.TimeoutError:
                raise _Failure("WRITE_UNKNOWN", "Complete original storage could not be confirmed.") from None
            except _Failure:
                raise _Failure("WRITE_UNKNOWN", "Complete original storage could not be confirmed.") from None
            if present == expected and expected > 0 and text == body.decode("utf-8"):
                return _success({"source_id": source_id, "processing": "unknown"})
            if (present == 0 and failure is not None and failure.code in _DEFINITIVE):
                # The readback proves the fresh session is empty, so the
                # definitive native rejection is the honest result.
                raise failure
            raise _Failure("WRITE_UNKNOWN", "Complete original storage could not be confirmed.")
        # The session id is assigned inside the deadline. Reattach it only once
        # known; TOO_LARGE/validation errors have not started a submission.
        try:
            result = await self._data_bounded(operation, timeout, write_started=started)
        except asyncio.CancelledError as exc:
            # Preserve the known resource locator for a northbound deadline or
            # caller cancellation without converting/suppressing cancellation.
            if started[0] and locator[0] is not None:
                exc.source_id = locator[0]
            raise
        if not result["ok"] and started[0]:
            result["error"]["source_id"] = locator[0]
        return result

    async def _reassemble(self, project: str, source_id: str, deadline: float) -> tuple[str, int, int]:
        """Return (payload_text, present_chunks, expected_chunks) for one session.

        The payload text is valid only when present == expected and expected > 0.
        """
        config = self._options
        chunks: dict[int, str] = {}
        expected: int | None = None
        total: int | None = None
        offset = 0
        while True:
            data = await self._v3("POST", "/v3/conversation/query", deadline, body={
                "session_id": source_id, "limit": _SEARCH_LIMIT, "offset": offset,
                "team_id": project, "agent_id": config["agent_id"], "user_id": config["owner_id"]})
            messages = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(messages, list):
                raise _Failure("BACKEND_ERROR", "MemoryCore query returned an invalid message list.")
            if total is None:
                total = data.get("total")
                if type(total) is not int or not 0 <= total <= 65536:
                    raise _Failure("BACKEND_ERROR", "MemoryCore query returned an invalid total.")
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str):
                    continue
                parsed = _HEADER.match(content)
                if parsed is None or parsed.group(1) != source_id:
                    continue
                index, count = int(parsed.group(3)), int(parsed.group(4))
                if (count < 1 or index >= count or index in chunks
                        or (expected is not None and count != expected)):
                    raise _Failure("READ_FAILED", "Stored chunk stream is inconsistent.")
                expected = count
                chunks[index] = content[parsed.end():]
            offset += len(messages)
            if not messages or offset >= total:
                break
        if expected is None:
            return "", 0, 0
        if len(chunks) != expected:
            return "", len(chunks), expected
        return "".join(chunks[index] for index in range(expected)), expected, expected

    def _validate_search(self, request: Any) -> tuple:
        if not isinstance(request, dict):
            raise _Failure("INVALID_ARGUMENT", "Search request must be an object.")
        if request.get("include_shared", False) is not False:
            raise _Failure("UNSUPPORTED", "Shared recall is not supported.")
        if any(name not in request for name in ("query", "features", "sources", "kinds", "limit")):
            raise _Failure("INVALID_ARGUMENT", "All five search fields are required.")
        query, features = request["query"], request["features"]
        if not isinstance(query, str) or type(features) is not list or not all(isinstance(x, str) for x in features):
            raise _Failure("INVALID_ARGUMENT", "query must be text and features a string array.")
        if not query.strip() and not any(feature.strip() for feature in features):
            raise _Failure("INVALID_ARGUMENT", "Nonempty retrieval information is required.")
        kinds, sources, limit = request["kinds"], request["sources"], request["limit"]
        if (type(kinds) is not list or not kinds or
                any(not isinstance(kind, str) or kind not in _KINDS for kind in kinds)):
            raise _Failure("INVALID_ARGUMENT", "kinds must be a nonempty subset of the supported kinds.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise _Failure("INVALID_ARGUMENT", "limit must be an integer from 1 through 20.")
        if type(sources) is not list or not sources:
            raise _Failure("INVALID_ARGUMENT", "sources must be a nonempty array.")
        projects = []
        for source in sources:
            if (not isinstance(source, dict) or not _text(source.get("project_id"))
                    or not _text(source.get("owner_id"))):
                raise _Failure("INVALID_ARGUMENT", "Each source requires nonempty project_id and owner_id.")
            if source["owner_id"] != self._options["owner_id"]:
                raise _Failure("ACCESS_DENIED", "Search source is outside the authenticated owner binding.")
            if source["project_id"] not in projects:
                projects.append(source["project_id"])
        # Fixed, information-preserving formatting; no intent generation. The
        # native query is a retrieval hint only, so it is bounded to the
        # gateway's 2048-character limit instead of failing the whole recall.
        native_query = query + ("\nFeatures:\n" + "\n".join(features) if features else "")
        return native_query.strip("\n")[:_NATIVE_QUERY_MAX], projects, list(dict.fromkeys(kinds)), limit

    async def search(self, request: dict, *, timeout=30.0) -> dict:
        async def operation(deadline):
            self._require_ready()
            query, projects, kinds, limit = self._validate_search(request)
            candidates: dict[str, list] = {}
            # Every requested source is explicitly queried. A single failure
            # fails the operation; it never becomes an empty/partial result.
            for project in projects:
                data = await self._v3("POST", "/v3/conversation/search", deadline, body={
                    "query": query, "limit": _SEARCH_LIMIT,
                    "team_id": project, "agent_id": self._options["agent_id"],
                    "user_id": self._options["owner_id"]})
                messages = data.get("messages") if isinstance(data, dict) else None
                if not isinstance(messages, list):
                    raise _Failure("BACKEND_ERROR", "MemoryCore search returned an invalid message list.")
                for hit in messages:
                    if not isinstance(hit, dict):
                        continue
                    content = hit.get("content")
                    if not isinstance(content, str):
                        continue
                    parsed = _HEADER.match(content)
                    if parsed is None or parsed.group(2) not in kinds:
                        continue
                    score = hit.get("score")
                    if (isinstance(score, bool) or not isinstance(score, (int, float))
                            or not math.isfinite(score)):
                        score = None
                    candidate = candidates.get(parsed.group(1))
                    if candidate is None:
                        candidates[parsed.group(1)] = [score, project, parsed.group(2)]
                    elif score is not None and (candidate[0] is None or score > candidate[0]):
                        candidate[0] = score
            ordered = sorted(candidates.items(), key=lambda pair: (
                pair[1][0] is not None, pair[1][0] if pair[1][0] is not None else 0), reverse=True)
            items, failed_reads = [], 0
            for source_id, (score, project, kind) in ordered:
                if len(items) >= limit:
                    break
                try:
                    record = await self._read_original(project, source_id, deadline)
                except _Failure:
                    # Individual read failures, including connection faults,
                    # use the contract's partial/READ_FAILED semantics.
                    failed_reads += 1
                    continue
                if (record["kind"] != kind or record["project_id"] != project
                        or record["owner_id"] != self._options["owner_id"]
                        or record["visibility"] != "private"):
                    # Envelope provenance disagrees with the search scope; the
                    # content cannot be authorized for return.
                    continue
                items.append({"content": record["content"], "content_type": "original",
                              "kind": record["kind"], "project_id": record["project_id"],
                              "owner_id": record["owner_id"], "task_id": record["task_id"],
                              "source_id": source_id, "score": score})
            if failed_reads and not items:
                raise _Failure("READ_FAILED", "All returnable hits failed original-content readback.")
            return _success({"items": items, "partial": bool(failed_reads)})
        return await self._data_bounded(operation, timeout)

    async def _read_original(self, project: str, source_id: str, deadline: float) -> dict:
        text, present, expected = await self._reassemble(project, source_id, deadline)
        if present != expected or expected == 0:
            raise _Failure("READ_FAILED", "Stored original could not be reassembled completely.")
        try:
            envelope = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
            if (not isinstance(envelope, dict) or type(envelope.get("schema_version")) is not int
                    or envelope["schema_version"] != 1 or envelope.get("source_id") != source_id):
                raise ValueError("Unsupported submission envelope")
            record = envelope["record"]
            if not isinstance(record, dict) or any(name not in record for name in _FIELDS):
                raise ValueError("Invalid original record")
            _json_value(record)
            if any(not _text(record[field]) for field in ("project_id", "owner_id", "task_id")):
                raise ValueError("Invalid original provenance")
            if type(record["content"]) not in (str, list, dict):
                raise ValueError("Invalid original content")
            if envelope.get("record_sha256") != hashlib.sha256(_encode(record)).hexdigest():
                raise ValueError("Original integrity check failed")
            return record
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, _Failure):
            raise _Failure("READ_FAILED", "Stored original integrity/provenance could not be verified.") from None

    async def close(self):
        async with self._lifecycle:
            self._state, self._message = "closed", "Closed."
            self._initialized = False
            # Existing data calls keep their immutable configuration/client
            # until settled. New calls fail NOT_READY; saved data is untouched.
            await self._idle.wait()
            if self._client is not None:
                try:
                    await self._client.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return _error("BACKEND_ERROR", "HTTP client resource release failed.")
                self._client = None
            self._options = None  # Drop the retained credential reference.
            self._provided_client = None
            return None
