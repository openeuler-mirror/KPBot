"""mem0 self-hosted HTTP adapter for the six-method memory backend contract.

Requires httpx only at initialize(), never reads SDK/default configuration,
and never starts a server. Pass ``client_factory(**httpx_client_kwargs)`` or
an already configured ``client`` for testing; clients expose async
request/aclose. Injected clients must honour the supplied timeout and must
not follow redirects.

Options: endpoint (credential-free HTTP(S) origin, required), owner_id
(injected by the trusted facade; binds record/search identity), max_input_bytes
(default 1048576 UTF-8 bytes of the complete serialized record), and
derive_memories (default true; task_process submissions additionally run the
backend's native LLM extraction in an isolated namespace).

Namespace scheme: every user_id is the compact JSON five-tuple
["kpbot-mem0", layer, kind, owner_id, project_id] with layer "raw" or
"derived". Raw originals are stored verbatim through infer=false and
confirmed by readback; the derived layer exists only for task_process and is
the only namespace the backend's add/update/delete deduplication can reach,
so confirmed originals and reviewed experiences are never overwritten.
Search fans out concurrently over the requested namespaces, keeps native
similarity scores (higher is more relevant, unscored hits last), and never
returns content it cannot attribute.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Callable
from urllib.parse import urlsplit

_KINDS = frozenset({"experience", "task_process"})
_FIELDS = ("kind", "project_id", "owner_id", "task_id", "visibility", "content")
_NAMESPACE_TAG = "kpbot-mem0"
_DEPENDENCY_NOTICE = (
    "Service reachable and verbatim storage available; embedding and LLM "
    "inference were not probed and no business records were created."
)


class _Failure(Exception):
    """Internal control-flow exception carrying a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _success(data: Any) -> dict:
    return {"ok": True, "data": data}


def _error(code: str, message: str, source_id: str | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message,
                                   "source_id": source_id}}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_value(value: Any) -> None:
    """Reject coercions (tuple, non-string keys, NaN) before encoding."""
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
    raise _Failure("INVALID_ARGUMENT",
                   "Input must contain only finite JSON-compatible values.")


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
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


def _namespace(layer: str, kind: str, owner_id: str, project_id: str) -> str:
    return json.dumps([_NAMESPACE_TAG, layer, kind, owner_id, project_id],
                      ensure_ascii=False, separators=(",", ":"))


class Mem0Backend(object):
    """Async adapter; one configured owner, any projects, private only."""

    def __init__(self, *, client_factory: Callable | None = None,
                 client: Any = None):
        if client_factory is not None and client is not None:
            raise ValueError(
                "Provide either client_factory or client, not both")
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
        return {"state": self._state, "message": self._message,
                "supports_shared": False}

    @staticmethod
    def _validate_timeout(timeout: Any) -> float:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise _Failure(
                "INVALID_ARGUMENT",
                "timeout must be a finite number greater than zero.")
        return float(timeout)

    async def _bounded(self, operation: Callable, timeout: Any, *,
                       source_id=None,
                       write_started: list | None = None) -> dict:
        try:
            seconds = self._validate_timeout(timeout)
            deadline = time.monotonic() + seconds
            return await asyncio.wait_for(operation(deadline), timeout=seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            uncertain = write_started is not None and write_started[0]
            if uncertain:
                return _error("WRITE_UNKNOWN",
                              "Write result is uncertain after timeout.",
                              source_id)
            return _error("TIMEOUT",
                          "Operation exceeded its total waiting deadline.")
        except _Failure as exc:
            return _error(exc.code, exc.message, source_id)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            uncertain = write_started is not None and write_started[0]
            if uncertain:
                return _error("WRITE_UNKNOWN",
                              "Write result could not be confirmed.",
                              source_id)
            return _error("INVALID_ARGUMENT",
                          "Input cannot be represented as supported JSON.")
        except Exception:
            # Do not expose native messages, response bodies, headers
            # or credentials.
            uncertain = write_started is not None and write_started[0]
            if uncertain:
                return _error("WRITE_UNKNOWN",
                              "Write result is not confirmed.",
                              source_id)
            return _error("BACKEND_ERROR",
                          "Unexpected backend operation failure.")

    async def _data_bounded(self, operation: Callable, timeout: Any,
                            **kwargs) -> dict:
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
        allowed = {"endpoint", "owner_id", "max_input_bytes", "derive_memories"}
        if set(options) - allowed:
            raise _Failure("INVALID_ARGUMENT",
                           "Unknown mem0 configuration option.")
        normalized = dict(options)
        endpoint = normalized.get("endpoint")
        if not _text(endpoint):
            raise _Failure("INVALID_ARGUMENT",
                           "endpoint must be a nonempty HTTP(S) origin.")
        try:
            parsed = urlsplit(endpoint)
            _ = parsed.port
        except ValueError:
            raise _Failure(
                "INVALID_ARGUMENT",
                "endpoint must be a valid HTTP(S) origin.") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")):
            raise _Failure("INVALID_ARGUMENT",
                           "endpoint must be a credential-free HTTP(S) origin.")
        normalized["endpoint"] = endpoint.rstrip("/")
        if not _text(normalized.get("owner_id")):
            raise _Failure("INVALID_ARGUMENT",
                           "owner_id must be a nonempty string.")
        size = normalized.setdefault("max_input_bytes", 1_048_576)
        if type(size) is not int or size <= 0:
            raise _Failure("INVALID_ARGUMENT",
                           "max_input_bytes must be a positive integer.")
        derive = normalized.setdefault("derive_memories", True)
        if type(derive) is not bool:
            raise _Failure("INVALID_ARGUMENT",
                           "derive_memories must be a boolean.")
        return normalized

    async def initialize(self, options: dict, *, timeout=30.0) -> dict:
        """Validate options, create the HTTP client, and probe the service.

        Args:
            options: Configuration mapping with the required ``endpoint``
                and ``owner_id`` plus optional ``max_input_bytes`` and
                ``derive_memories``.
            timeout: Total seconds allowed for the whole initialization.

        Returns:
            The standard result envelope with the backend status payload.
        """
        async def operation(deadline):
            config = self._normalize_options(options)
            async with self._lifecycle:
                if self._state == "closed":
                    raise _Failure(
                        "NOT_READY",
                        "Closed adapters cannot be initialized again.")
                if self._initialized:
                    if config != self._options:
                        raise _Failure(
                            "INVALID_ARGUMENT",
                            "Use a new instance to change configuration.")
                    await self._probe(deadline)
                else:
                    if (self._client is not None
                            and self._client is not self._provided_client):
                        await self._client.aclose()
                    if self._provided_client is not None:
                        self._client = self._provided_client
                    else:
                        factory = self._factory
                        if factory is None:
                            try:
                                import httpx
                            except ImportError:
                                raise _Failure(
                                    "UNAVAILABLE",
                                    "httpx is required to initialize "
                                    "this backend.")
                            factory = httpx.AsyncClient
                        self._client = factory(
                            base_url=config["endpoint"],
                            timeout=self._remaining(deadline),
                            follow_redirects=False, trust_env=False)
                    await self._probe(deadline)
                    self._options = config
                    self._initialized = True
                self._state = "ready"
                self._message = _DEPENDENCY_NOTICE
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._state != "closed" and
                result["error"]["code"] not in ("INVALID_ARGUMENT",
                                                "NOT_READY")):
            self._state = "unavailable"
            self._message = result["error"]["message"]
        return result

    async def _probe(self, deadline: float) -> None:
        data = await self._request("GET", "/openapi.json", deadline)
        try:
            schema = data["components"]["schemas"]["MemoryCreate"]["properties"]
        except (KeyError, TypeError):
            schema = None
        if not isinstance(schema, dict) or "infer" not in schema:
            raise _Failure(
                "UNAVAILABLE",
                "mem0 service does not expose verbatim storage (infer).")

    async def status(self, *, timeout=30.0) -> dict:
        """Report the backend state after re-probing the service.

        Args:
            timeout: Total seconds allowed for the whole status operation.

        Returns:
            The standard result envelope with the status payload.
        """
        async def operation(deadline):
            async with self._lifecycle:
                if self._initialized and self._state != "closed":
                    try:
                        await self._probe(deadline)
                    except _Failure as exc:
                        self._state = "unavailable"
                        self._message = exc.message
                    else:
                        self._state = "ready"
                        self._message = _DEPENDENCY_NOTICE
                return _success(self._status_data())
        result = await self._bounded(operation, timeout)
        if (not result["ok"] and self._initialized and self._state != "closed"
                and result["error"]["code"] != "INVALID_ARGUMENT"):
            self._state = "unavailable"
            self._message = result["error"]["message"]
        return result

    def _require_ready(self) -> None:
        if not self._initialized or self._state == "closed":
            raise _Failure("NOT_READY",
                           "Backend is not initialized or has been closed.")

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    async def _request(self, method: str, path: str, deadline: float, *,
                       write_started: list | None = None, **kwargs):
        remaining = self._remaining(deadline)
        if write_started is not None:
            write_started[0] = True
        try:
            response = await self._client.request(
                method, path, timeout=remaining, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
                raise asyncio.TimeoutError from None
            raise _Failure("UNAVAILABLE",
                           "mem0 HTTP connection failed.") from None
        status = response.status_code
        if status == 504:
            raise asyncio.TimeoutError
        if status in (401, 403):
            raise _Failure("ACCESS_DENIED",
                           "mem0 authentication or access was denied.")
        if 300 <= status < 400:
            raise _Failure("BACKEND_ERROR", "mem0 redirects are not followed.")
        try:
            data = response.json()
        except Exception:
            raise _Failure(
                "BACKEND_ERROR",
                "mem0 returned an invalid JSON response.") from None
        if not (200 <= status < 300):
            if status == 413:
                raise _Failure("TOO_LARGE",
                               "mem0 rejected the request as too large.")
            if status == 503:
                raise _Failure("UNAVAILABLE",
                               "mem0 service is unavailable.")
            if status in (400, 422):
                raise _Failure("INVALID_ARGUMENT",
                               "mem0 rejected the request payload.")
            raise _Failure("BACKEND_ERROR",
                           f"mem0 request failed (HTTP {status}).")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise _Failure("BACKEND_ERROR", "mem0 response must be an object.")
        return data

    def _validate_record(self, record: Any, kind: str) -> None:
        if (not isinstance(record, dict)
                or any(name not in record for name in _FIELDS)):
            raise _Failure("INVALID_ARGUMENT",
                           "All six record fields are required.")
        if record["kind"] != kind:
            raise _Failure("INVALID_ARGUMENT",
                           "Record kind does not match the storage method.")
        for field in ("project_id", "owner_id", "task_id"):
            if not _text(record[field]):
                raise _Failure("INVALID_ARGUMENT",
                               f"{field} must be a nonempty string.")
        if record["visibility"] != "private":
            raise _Failure("UNSUPPORTED", "Only private records are supported.")
        if record["owner_id"] != self._options["owner_id"]:
            raise _Failure(
                "ACCESS_DENIED",
                "Record owner is outside the authenticated binding.")
        if type(record["content"]) not in (str, dict, list):
            raise _Failure(
                "INVALID_ARGUMENT",
                "content must be text, a JSON object, or an array.")
        _json_value(record)

    async def store_experience(self, record: dict, *, timeout=30.0) -> dict:
        """Store an experience record verbatim in the raw namespace.

        Args:
            record: The six-field experience record to persist.
            timeout: Total seconds allowed for the whole store operation.

        Returns:
            The standard result envelope with source_id and processing.
        """
        return await self._store(record, "experience", timeout)

    async def store_process(self, record: dict, *, timeout=30.0) -> dict:
        """Store a task_process record and optionally derive memories.

        Args:
            record: The six-field task_process record to persist.
            timeout: Total seconds allowed for the whole store operation.

        Returns:
            The standard result envelope with source_id and processing.
        """
        return await self._store(record, "task_process", timeout)

    async def _store(self, record: dict, kind: str, timeout: Any) -> dict:
        started = [False]
        locator = [None]

        async def operation(deadline):
            self._require_ready()
            self._validate_record(record, kind)
            frozen = json.loads(_encode(record))
            text = _encode(frozen).decode("utf-8")
            maximum = self._options["max_input_bytes"]
            if len(text.encode("utf-8")) > maximum:
                raise _Failure(
                    "TOO_LARGE",
                    f"Complete serialized record exceeds {maximum} "
                    "UTF-8 bytes.")
            owner, project = frozen["owner_id"], frozen["project_id"]
            metadata = {"kpbot_kind": kind, "project_id": project,
                        "owner_id": owner, "task_id": frozen["task_id"]}
            memory_id = None
            try:
                payload = {
                    "messages": [{"role": "user", "content": text}],
                    "user_id": _namespace("raw", kind, owner, project),
                    "metadata": metadata,
                    "infer": False,
                }
                data = await self._request("POST", "/memories", deadline,
                                           write_started=started,
                                           json=payload)
                results = (data.get("results")
                           if isinstance(data, dict) else None)
                if isinstance(results, list):
                    for item in results:
                        if (isinstance(item, dict)
                                and item.get("event") == "ADD"
                                and _text(item.get("id"))):
                            memory_id = item["id"]
                            break
            except (asyncio.TimeoutError, _Failure):
                # A native error, even 4xx, is no proof the write never reached
                # storage; without a confirmed locator nothing can be verified.
                pass
            if memory_id is None:
                # Nothing was confirmed; the write may still have partially
                # executed.
                raise _Failure(
                    "WRITE_UNKNOWN",
                    "Complete original storage could not be confirmed.")
            locator[0] = memory_id
            try:
                stored = await self._request(
                    "GET", f"/memories/{memory_id}", deadline)
            except (asyncio.TimeoutError, _Failure):
                raise _Failure(
                    "WRITE_UNKNOWN",
                    "Complete original storage could not be "
                    "confirmed.") from None
            if (not isinstance(stored, dict) or stored.get("id") != memory_id
                    or stored.get("memory") != text):
                raise _Failure(
                    "WRITE_UNKNOWN",
                    "Persisted original did not match the full "
                    "submitted input.")
            processing = "ready"
            if kind == "task_process" and self._options["derive_memories"]:
                if deadline - time.monotonic() <= 0:
                    processing = "unknown"
                else:
                    derive_meta = dict(metadata)
                    derive_meta["kpbot_source_id"] = memory_id
                    derive_payload = {
                        "messages": self._derive_messages(
                            frozen["content"]),
                        "user_id": _namespace("derived", "task_process",
                                              owner, project),
                        "metadata": derive_meta,
                        "infer": True,
                    }
                    try:
                        await self._request("POST", "/memories",
                                            deadline, json=derive_payload)
                    except asyncio.TimeoutError:
                        processing = "unknown"
                    except _Failure:
                        processing = "failed"
            return _success({"source_id": memory_id, "processing": processing})

        try:
            result = await self._data_bounded(operation, timeout,
                                              write_started=started)
        except asyncio.CancelledError as exc:
            if started[0] and locator[0] is not None:
                exc.source_id = locator[0]
            raise
        if not result["ok"] and started[0]:
            result["error"]["source_id"] = locator[0]
        return result

    @staticmethod
    def _derive_messages(content: Any) -> list:
        if (isinstance(content, list) and content and
                all(isinstance(item, dict) and _text(item.get("role"))
                    and _text(item.get("content"))
                    for item in content)):
            return [{"role": item["role"], "content": item["content"]}
                    for item in content]
        return [{"role": "user", "content": _encode(content).decode("utf-8")}]

    def _validate_search(self, request: Any) -> tuple:
        if not isinstance(request, dict):
            raise _Failure("INVALID_ARGUMENT",
                           "Search request must be an object.")
        if request.get("include_shared", False) is not False:
            raise _Failure("UNSUPPORTED", "Shared recall is not supported.")
        if any(name not in request for name in
               ("query", "features", "sources", "kinds", "limit")):
            raise _Failure("INVALID_ARGUMENT",
                           "All five search fields are required.")
        query, features = request["query"], request["features"]
        if (not isinstance(query, str) or type(features) is not list
                or not all(isinstance(x, str) for x in features)):
            raise _Failure(
                "INVALID_ARGUMENT",
                "query must be text and features a string array.")
        if (not query.strip()
                and not any(feature.strip() for feature in features)):
            raise _Failure(
                "INVALID_ARGUMENT",
                "Nonempty retrieval information is required.")
        kinds = request["kinds"]
        sources = request["sources"]
        limit = request["limit"]
        if (type(kinds) is not list or not kinds
                or any(not isinstance(kind, str) or kind not in _KINDS
                       for kind in kinds)):
            raise _Failure(
                "INVALID_ARGUMENT",
                "kinds must be a nonempty subset of the supported kinds.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise _Failure("INVALID_ARGUMENT",
                           "limit must be an integer from 1 through 20.")
        if type(sources) is not list or not sources:
            raise _Failure("INVALID_ARGUMENT",
                           "sources must be a nonempty array.")
        projects = []
        for source in sources:
            if (not isinstance(source, dict)
                    or not _text(source.get("project_id"))
                    or not _text(source.get("owner_id"))):
                raise _Failure(
                    "INVALID_ARGUMENT",
                    "Each source requires nonempty project_id and owner_id.")
            if source["owner_id"] != self._options["owner_id"]:
                raise _Failure(
                    "ACCESS_DENIED",
                    "Search source is outside the authenticated owner "
                    "binding.")
            if source["project_id"] not in projects:
                projects.append(source["project_id"])
        # Fixed, information-preserving formatting; no intent generation.
        native_query = query + ("\n" + "\n".join(features) if features else "")
        return native_query, projects, list(dict.fromkeys(kinds)), limit

    async def search(self, request: dict, *, timeout=30.0) -> dict:
        """Search the requested namespaces for attributable memories.

        Args:
            request: Search request carrying query, features, sources,
                kinds and limit.
            timeout: Total seconds allowed for the whole search operation.

        Returns:
            The standard result envelope with the items and partial flag.
        """
        async def operation(deadline):
            self._require_ready()
            validated = self._validate_search(request)
            native_query, projects, kinds, limit = validated
            owner = self._options["owner_id"]
            targets = {}
            for project in projects:
                for kind in kinds:
                    namespace = _namespace("raw", kind, owner, project)
                    targets[namespace] = ("raw", kind, project)
                if "task_process" in kinds:
                    namespace = _namespace("derived", "task_process",
                                           owner, project)
                    targets[namespace] = ("derived", "task_process",
                                          project)
            responses = await asyncio.gather(*[
                self._request("POST", "/search", deadline,
                              json={"query": native_query,
                                    "user_id": namespace})
                for namespace in targets], return_exceptions=True)
            for response in responses:
                # Every requested source is explicitly queried. A single failure
                # fails the operation; it never becomes an empty/partial result.
                if isinstance(response, BaseException):
                    raise response
            hits, skipped = [], 0
            for namespace, response in zip(targets, responses):
                layer, kind, project = targets[namespace]
                results = (response.get("results")
                           if isinstance(response, dict) else None)
                if not isinstance(results, list):
                    raise _Failure(
                        "BACKEND_ERROR",
                        "mem0 search returned an invalid result list.")
                for mem in results:
                    item = self._item(mem, layer, kind, project, owner)
                    if item is None:
                        skipped += 1
                        continue
                    hits.append(item)
            ordered = sorted(
                hits,
                key=lambda item: (
                    item["score"] is None,
                    -item["score"] if item["score"] is not None else 0.0))
            items = ordered[:limit]
            if skipped and not items:
                raise _Failure(
                    "READ_FAILED",
                    "All returnable hits failed original-content "
                    "verification.")
            return _success({"items": items, "partial": bool(skipped)})
        return await self._data_bounded(operation, timeout)

    def _item(self, mem: Any, layer: str, kind: str, project: str,
              owner: str) -> dict | None:
        if not isinstance(mem, dict) or not _text(mem.get("id")):
            return None
        meta = mem.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        score = mem.get("score")
        if (type(score) not in (int, float) or isinstance(score, bool)
                or not math.isfinite(score)):
            score = None
        if meta and (meta.get("kpbot_kind") != kind
                     or meta.get("project_id") != project
                     or meta.get("owner_id") != owner):
            return None
        if layer == "derived":
            if not _text(mem.get("memory")):
                return None
            task_id = meta.get("task_id")
            source_id = meta.get("kpbot_source_id")
            return {"content": mem["memory"], "content_type": "derived",
                    "kind": "task_process", "project_id": project,
                    "owner_id": owner,
                    "task_id": task_id if _text(task_id) else None,
                    "source_id": source_id if _text(source_id) else None,
                    "score": score}
        text = mem.get("memory")
        maximum = self._options["max_input_bytes"]
        if (not isinstance(text, str)
                or len(text.encode("utf-8")) > maximum):
            return None
        try:
            frozen = json.loads(text, object_pairs_hook=_pairs,
                                parse_constant=_reject_constant)
        except (ValueError, TypeError, RecursionError):
            return None
        if (not isinstance(frozen, dict)
                or any(name not in frozen for name in _FIELDS)
                or frozen["kind"] != kind or frozen["project_id"] != project
                or frozen["owner_id"] != owner
                or frozen["visibility"] != "private"
                or not _text(frozen["task_id"])
                or type(frozen["content"]) not in (str, dict, list)):
            return None
        try:
            _json_value(frozen["content"])
        except _Failure:
            return None
        return {"content": frozen["content"], "content_type": "original",
                "kind": kind, "project_id": project, "owner_id": owner,
                "task_id": frozen["task_id"], "source_id": mem["id"],
                "score": score}

    async def close(self):
        """Close the HTTP client after in-flight operations settle.

        Returns:
            None on success, or the standard error envelope when client
            release fails.
        """
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
                    return _error("BACKEND_ERROR",
                                  "HTTP client resource release failed.")
                self._client = None
            self._options = None
            self._provided_client = None
            return None
