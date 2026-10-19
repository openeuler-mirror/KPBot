"""OpenViking HTTP adapter for the six-method memory backend contract.

Requires httpx only at initialize(), never reads SDK/default configuration, and
never starts a server. Pass ``client_factory(**httpx_client_kwargs)`` or an
already configured ``client`` for testing; clients expose async request/aclose.
Injected clients must honour the supplied timeout and must not follow redirects.

Options: endpoint (loopback HTTP(S), default http://127.0.0.1:1933), api_key,
account_id, user_id (provider identity), owner_id (KPBot identity),
processing_mode (vectors_only by default, or semantic_and_vectors), and
max_input_bytes (default 1048576 UTF-8 bytes of the complete resource envelope).
All four identity/credential options are required. Each owner/project/kind has
a deterministic private directory; each submission gets a new UUID resource.
Both content kinds retain the complete reversible JSON input, not a summary.
Native indexing may truncate its model input; stored original content is never
truncated. No local content store, operation journal, or write retry is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

_KINDS = frozenset({"experience", "task_process"})
_MODES = frozenset({"vectors_only", "semantic_and_vectors"})
_FIELDS = ("kind", "project_id", "owner_id", "task_id", "visibility", "content")
_DEPENDENCY_NOTICE = (
    "Authenticated service/storage readiness checked; embedding inference and "
    "per-record index visibility are not probed."
)


class _Failure(Exception):
    def __init__(self, code: str, message: str, *, native_code: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.native_code = native_code


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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON constant")


class OpenVikingBackend:
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
    def _normalize_options(options: Any) -> dict:
        if not isinstance(options, dict):
            raise _Failure("INVALID_ARGUMENT", "options must be an object.")
        allowed = {"endpoint", "api_key", "account_id", "user_id", "owner_id",
                   "processing_mode", "max_input_bytes"}
        if set(options) - allowed:
            raise _Failure("INVALID_ARGUMENT", "Unknown OpenViking configuration option.")
        normalized = dict(options)
        normalized.setdefault("endpoint", "http://127.0.0.1:1933")
        normalized.setdefault("processing_mode", "vectors_only")
        normalized.setdefault("max_input_bytes", 1_048_576)
        for name in ("api_key", "account_id", "user_id", "owner_id"):
            if not _text(normalized.get(name)):
                raise _Failure("INVALID_ARGUMENT", f"{name} must be a nonempty string.")
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized["api_key"]):
            raise _Failure("INVALID_ARGUMENT", "api_key contains invalid header characters.")
        user = normalized["user_id"]
        if not re.fullmatch(r"[A-Za-z0-9_@+.-]+", user) or user in (".", ".."):
            raise _Failure("INVALID_ARGUMENT", "user_id must be a safe provider URI component.")
        endpoint = normalized["endpoint"]
        if not isinstance(endpoint, str):
            raise _Failure("INVALID_ARGUMENT", "endpoint must be a loopback HTTP(S) URL.")
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            local = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
            _ = parsed.port
        except ValueError:
            local = False
        if (not local or parsed.scheme not in ("http", "https") or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise _Failure("INVALID_ARGUMENT", "endpoint must be a credential-free loopback HTTP(S) origin.")
        normalized["endpoint"] = endpoint.rstrip("/")
        if normalized["processing_mode"] not in _MODES:
            raise _Failure("INVALID_ARGUMENT", "processing_mode must be vectors_only or semantic_and_vectors.")
        size = normalized["max_input_bytes"]
        if type(size) is not int or size <= 0:
            raise _Failure("INVALID_ARGUMENT", "max_input_bytes must be a positive integer.")
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
                    await self._probe(config, deadline)
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
                                               headers={"X-API-Key": config["api_key"]},
                                               timeout=self._remaining(deadline),
                                               follow_redirects=False, trust_env=False)
                    await self._probe(config, deadline)
                    self._options = config
                    self._initialized = True
                self._state = "ready"
                self._message = self._notice(config)
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._state != "closed" and
                result["error"]["code"] not in ("INVALID_ARGUMENT", "NOT_READY")):
            self._state = "unavailable"
            self._message = result["error"]["message"]
        return result

    @staticmethod
    def _notice(config: dict) -> str:
        message = _DEPENDENCY_NOTICE
        if config["processing_mode"] == "semantic_and_vectors":
            message += " VLM inference is also unverified."
        return message

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    async def _request(self, method: str, path: str, deadline: float, *, raw=False,
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
            raise _Failure("UNAVAILABLE", "OpenViking HTTP connection failed.") from None
        status = response.status_code
        if status == 504:
            raise asyncio.TimeoutError
        if status in (401, 403):
            raise _Failure("ACCESS_DENIED", "OpenViking authentication or access was denied.")
        if 300 <= status < 400:
            raise _Failure("BACKEND_ERROR", "OpenViking redirects are not followed.")
        if raw and 200 <= status < 300:
            return bytes(response.content)
        try:
            data = response.json()
        except Exception:
            raise _Failure("BACKEND_ERROR", "OpenViking returned an invalid JSON response.") from None
        if not isinstance(data, dict):
            raise _Failure("BACKEND_ERROR", "OpenViking response must be an object.")
        if not (200 <= status < 300) or data.get("status") == "error":
            native = data.get("error", {})
            native = native.get("code", "") if isinstance(native, dict) else ""
            if not isinstance(native, str):
                native = ""
            mapped = {"INVALID_ARGUMENT": "INVALID_ARGUMENT", "INVALID_URI": "INVALID_ARGUMENT",
                      "UNAUTHENTICATED": "ACCESS_DENIED", "PERMISSION_DENIED": "ACCESS_DENIED",
                      "NOT_INITIALIZED": "UNAVAILABLE", "UNAVAILABLE": "UNAVAILABLE",
                      "EMBEDDING_FAILED": "UNAVAILABLE", "VLM_FAILED": "UNAVAILABLE",
                      "UNIMPLEMENTED": "UNSUPPORTED"}.get(native, "BACKEND_ERROR")
            if native == "DEADLINE_EXCEEDED":
                raise asyncio.TimeoutError
            if status == 413:
                mapped = "TOO_LARGE"
            elif status == 503:
                mapped = "UNAVAILABLE"
            elif status in (400, 422) and mapped == "BACKEND_ERROR":
                mapped = "INVALID_ARGUMENT"
            raise _Failure(mapped, f"OpenViking request failed (HTTP {status}).", native_code=native)
        return data

    async def _result(self, method: str, path: str, deadline: float, **kwargs):
        data = await self._request(method, path, deadline, **kwargs)
        if data.get("status") != "ok" or "result" not in data:
            raise _Failure("BACKEND_ERROR", "OpenViking response lacks a successful result.")
        return data["result"]

    async def _probe(self, config: dict, deadline: float) -> None:
        health = await self._request("GET", "/health", deadline)
        if health.get("healthy") is not True:
            raise _Failure("UNAVAILABLE", "OpenViking health check did not pass.")
        status = await self._result("GET", "/api/v1/system/status", deadline)
        if not isinstance(status, dict) or status.get("initialized") is not True:
            raise _Failure("UNAVAILABLE", "OpenViking service is not initialized.")
        if (health.get("auth_mode") != "api_key" or health.get("role") != "user"
                or health.get("account_id") != config["account_id"]
                or health.get("user_id") != config["user_id"]
                or status.get("user") != config["user_id"]):
            raise _Failure("ACCESS_DENIED", "Expected ordinary-user API-key identity was not verified.")
        ready = await self._request("GET", "/ready", deadline)
        checks = ready.get("checks", {})
        if (ready.get("status") != "ready" or not isinstance(checks, dict)
                or checks.get("embedding") != "ok" or checks.get("vectordb") != "ok"):
            raise _Failure("UNAVAILABLE", "Required OpenViking storage/embedding readiness did not pass.")
        await self._result("GET", "/api/v1/fs/ls", deadline,
                           params={"uri": f"viking://user/{config['user_id']}/resources", "limit": 1})

    async def status(self, *, timeout=30.0) -> dict:
        async def operation(deadline):
            async with self._lifecycle:
                if self._initialized and self._state != "closed":
                    try:
                        await self._probe(self._options, deadline)
                    except _Failure as exc:
                        self._state, self._message = "unavailable", exc.message
                    else:
                        self._state, self._message = "ready", self._notice(self._options)
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._initialized and self._state != "closed" and
                result["error"]["code"] != "INVALID_ARGUMENT"):
            self._state, self._message = "unavailable", result["error"]["message"]
        return result

    def _require_ready(self):
        if not self._initialized or self._state == "closed":
            raise _Failure("NOT_READY", "Backend is not initialized or has been closed.")

    def _directory(self, project: str, kind: str) -> str:
        config = self._options
        return (f"viking://user/{config['user_id']}/resources/kpbot/"
                f"{_digest(config['owner_id'])}/{_digest(project)}/{kind}")

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
            uri = f"{self._directory(frozen['project_id'], kind)}/{uuid.uuid4().hex}.json"
            locator[0] = uri
            envelope = {"schema_version": 1, "source_id": uri, "record": frozen,
                        "record_sha256": hashlib.sha256(_encode(frozen)).hexdigest()}
            body = _encode(envelope)
            maximum = self._options["max_input_bytes"]
            if len(body) > maximum:
                raise _Failure("TOO_LARGE", f"Complete resource envelope exceeds {maximum} UTF-8 bytes.")
            failure = None
            result = None
            try:
                result = await self._result("POST", "/api/v1/content/write", deadline,
                                            write_started=started,
                                            json={"uri": uri, "content": body.decode("utf-8"),
                                                  "mode": "create", "wait": True,
                                                  "timeout": self._remaining(deadline),
                                                  "processing_mode": self._options["processing_mode"]})
            except _Failure as exc:
                # A native HTTP/error code carries no execution-stage proof.
                # Even 400/413/403 may follow a write or a failed rollback;
                # confirm the original instead of claiming no side effects.
                failure = exc
            except asyncio.TimeoutError:
                # Native timeouts can arrive before our whole-call deadline.
                # Use only its remaining budget for a read, never another write.
                failure = _Failure("TIMEOUT", "Native write result is uncertain.")
            try:
                persisted = await self._request("GET", "/api/v1/content/download", deadline,
                                                raw=True, params={"uri": uri})
                if persisted != body:
                    raise _Failure("WRITE_UNKNOWN", "Persisted original did not match the full submitted input.")
            except _Failure:
                raise _Failure("WRITE_UNKNOWN", "Complete original storage could not be confirmed.") from None
            if failure is not None:
                processing = ("failed" if failure.native_code in
                              {"EMBEDDING_FAILED", "VLM_FAILED", "PROCESSING_ERROR", "REFRESH_FAILED"}
                              else "unknown")
            else:
                processing = self._processing(result)
            return _success({"source_id": uri, "processing": processing})
        # The URI is assigned inside the deadline. Reattach it only once known;
        # TOO_LARGE/validation errors have not started a submission.
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

    def _processing(self, result: Any) -> str:
        if not isinstance(result, dict):
            return "unknown"
        statuses = [result.get("vector_status")]
        if self._options["processing_mode"] == "semantic_and_vectors":
            statuses.append(result.get("semantic_status"))
        if "failed" in statuses:
            return "failed"
        if any(status in ("queued", "deferred") for status in statuses):
            return "pending"
        if all(status == "complete" for status in statuses):
            return "ready"
        return "unknown"

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
        # Fixed, information-preserving formatting; no intent generation.
        native_query = query + ("\nFeatures:\n" + "\n".join(features) if features else "")
        return native_query, projects, list(dict.fromkeys(kinds)), limit

    async def search(self, request: dict, *, timeout=30.0) -> dict:
        async def operation(deadline):
            self._require_ready()
            query, projects, kinds, limit = self._validate_search(request)
            scopes = {self._directory(project, kind): (project, kind)
                      for project in projects for kind in kinds}
            hits = {}
            # Every requested source is explicitly queried. A single failure
            # fails the operation; it never becomes an empty/partial result.
            for project in projects:
                targets = [self._directory(project, kind) for kind in kinds]
                result = await self._result("POST", "/api/v1/search/find", deadline,
                                            json={"query": query, "target_uri": targets,
                                                  "context_type": "resource", "level": 2,
                                                  "limit": limit, "read_content": False})
                if not isinstance(result, dict) or type(result.get("resources")) is not list:
                    raise _Failure("BACKEND_ERROR", "OpenViking find returned an invalid resource list.")
                for hit in result["resources"]:
                    if not isinstance(hit, dict):
                        continue
                    uri = hit.get("uri")
                    if (not isinstance(uri, str) or type(hit.get("level")) is not int
                            or hit["level"] != 2 or hit.get("context_type") != "resource"):
                        continue
                    directory, _, filename = uri.rpartition("/")
                    if directory not in scopes or not re.fullmatch(r"[a-f0-9]{32}\.json", filename):
                        continue
                    score = hit.get("score")
                    if type(score) not in (int, float) or not math.isfinite(score):
                        score = None
                    previous = hits.get(uri)
                    if previous is None or (score is not None and
                                             (previous[0] is None or score > previous[0])):
                        hits[uri] = (score, scopes[directory])
            ordered = sorted(hits.items(), key=lambda pair: (
                pair[1][0] is not None, pair[1][0] if pair[1][0] is not None else 0), reverse=True)
            items, failed_reads = [], 0
            for uri, (score, (project, kind)) in ordered:
                try:
                    raw = await self._request("GET", "/api/v1/content/download", deadline,
                                              raw=True, params={"uri": uri})
                    record = self._decode_original(raw, uri)
                except _Failure:
                    # Individual read failures, including connection faults,
                    # use the contract's partial/READ_FAILED semantics.
                    failed_reads += 1
                    continue
                if (record["project_id"] != project or record["kind"] != kind or
                        record["owner_id"] != self._options["owner_id"] or record["visibility"] != "private"):
                    continue
                items.append({"content": record["content"], "content_type": "original",
                              "kind": kind, "project_id": project,
                              "owner_id": record["owner_id"], "task_id": record["task_id"],
                              "source_id": uri, "score": score})
            if failed_reads and not items:
                raise _Failure("READ_FAILED", "All returnable hits failed original-content readback.")
            return _success({"items": items[:limit], "partial": bool(failed_reads)})
        return await self._data_bounded(operation, timeout)

    def _decode_original(self, raw: bytes, uri: str) -> dict:
        try:
            if len(raw) > self._options["max_input_bytes"]:
                raise ValueError("Resource too large")
            envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                  parse_constant=_reject_constant)
            if (not isinstance(envelope, dict) or type(envelope.get("schema_version")) is not int
                    or envelope["schema_version"] != 1 or envelope.get("source_id") != uri):
                raise ValueError("Unsupported resource envelope")
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
            raise _Failure("READ_FAILED", "Stored resource integrity/provenance could not be verified.") from None

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
