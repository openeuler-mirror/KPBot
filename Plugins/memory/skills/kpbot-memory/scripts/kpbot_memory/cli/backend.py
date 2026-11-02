"""One-shot memory backend CLI for OpenViking, TencentDB or mem0.

Every invocation creates a fresh client, initializes it from explicit trusted
configuration, executes one operation, and closes the client in finally. There
is no cross-process initialization state, automatic transcript collection,
service startup/shutdown, write retry, or token-based login in this namespace.

JSON inputs are complete northbound records/requests. remember --content-file
wraps an explicitly supplied UTF-8 text file without changing any characters.
options.max_input_bytes bounds bytes read (default 1 MiB); the adapter also
checks its complete serialized resource envelope, whose metadata adds bytes.
The async initialize + requested operation share --timeout; close is cleanup
and has no contract timeout. No input or configuration values are logged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from time import monotonic
from typing import Any

from ..backend_config import BackendConfigError, clone_json, load_config
from ..northbound import NorthboundMemory

DEFAULT_MAX_INPUT_BYTES = 1_048_576
OPERATIONS = ("initialize", "status", "remember", "recall", "close")
_PROGRESS_INTERVAL = 1.0


class _Progress:
    """Human-only stage reporting; never render backend or configuration values."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _write(self, text: str) -> None:
        if self.enabled:
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except (OSError, ValueError):
                # Progress is observational; a closed stderr must not change
                # an operation's JSON result or interrupt client cleanup.
                self.enabled = False

    def _line(self, stage: str, state: str, started: float) -> None:
        self._write(f"[memory] {stage}: {state}; elapsed={monotonic() - started:.1f}s\n")

    async def run(self, stage: str, operation):
        if not self.enabled:
            return await operation
        started = monotonic()
        self._line(stage, "started", started)

        async def heartbeat():
            while True:
                await asyncio.sleep(_PROGRESS_INTERVAL)
                self._line(stage, "waiting", started)

        ticker = asyncio.create_task(heartbeat(), name="memory-cli-progress")
        try:
            result = await operation
            state = "failed" if isinstance(result, dict) and result.get("ok") is False else "completed"
            self._line(stage, state, started)
            return result
        except BaseException:
            self._line(stage, "interrupted", started)
            raise
        finally:
            ticker.cancel()
            # Consume the ticker's cancellation without swallowing a fresh
            # cancellation of this command while the ticker is being joined.
            await asyncio.gather(ticker, return_exceptions=True)

    def failure_hint(self, result: dict | None, backend: str | None) -> None:
        if not self.enabled or not isinstance(result, dict) or result.get("ok") is not False:
            return
        code = result.get("error", {}).get("code")
        hints = {
            "INVALID_ARGUMENT": "Check backend --help, the trusted configuration, and input JSON fields.",
            "ACCESS_DENIED": "Check configuration ownership/mode 0600, credentials, and allowed user/project scope.",
            "UNSUPPORTED": "Use only the selected backend's supported options; shared recall is not available.",
            "NOT_READY": "Check that initialization succeeds before using the memory client.",
            "TIMEOUT": "Check service reachability and model/storage latency before adjusting --timeout.",
            "WRITE_UNKNOWN": "Do not automatically resubmit; use the JSON source_id to investigate the original write.",
            "TOO_LARGE": "Check the configured byte limit and backend request limits; do not truncate original content.",
            "READ_FAILED": "Check backend storage and record availability; do not treat this as an empty search.",
            "BACKEND_ERROR": "Inspect the backend service logs and installed dependencies for the failed operation.",
        }
        if code == "UNAVAILABLE":
            hint = {
                "openviking": "Check the OpenViking service, account/user credentials, and embedding/index readiness.",
                "mem0": "Check the mem0 REST service, API key, and database availability.",
                "tencentdb": "Check MemoryCore 2.0.3, service identity/credentials, and BM25 or embedding availability.",
            }.get(backend, "Check the selected backend service and installed client dependencies.")
            hint += " Ensure httpx==0.28.1 is installed in the Python interpreter running this command; see the bundled backend guide."
        else:
            hint = hints.get(code, "Inspect the JSON error and selected backend documentation for the failed operation.")
        self._write(f"[memory] Next step: {hint}\n")

__all__ = ["main", "build_parser", "create_backend"]


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "source_id": None}}


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse normally echoes rejected values, which could be a secret.
        raise BackendConfigError("invalid backend command arguments; use backend --help")


def _positive_timeout(raw: str) -> float:
    try:
        result = float(raw)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("timeout must be a finite positive number") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="kpbot memory backend",
        description=(
            "One-shot memory backend client (OpenViking, TencentDB or mem0): initialize, "
            "run one operation, then close. CLI invocations have no cross-process "
            "state; initialize/status are fresh readiness probes, close releases only "
            "this invocation's client, not the service."
        ),
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True, help="explicit trusted owner-owned 0600 backend JSON file")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--input", help="complete strict JSON record/request file, or - for stdin")
    inputs.add_argument("--content-file", help="remember only: complete UTF-8 task-process text file, or - for stdin")
    parser.add_argument("--kind", choices=("task_process",), help="required for text submission: task_process")
    parser.add_argument("--project-id", help="required for text submission: authorized project ID")
    parser.add_argument("--task-id", help="required for text submission: task ID")
    parser.add_argument("--timeout", type=_positive_timeout, default=30.0,
                        help="finite positive seconds for initialize + operation (default 30; excludes close cleanup)")
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument("--progress", dest="progress", action="store_true",
                          help="show stages and elapsed waiting time on stderr (default when stderr is a TTY)")
    progress.add_argument("--no-progress", dest="progress", action="store_false",
                          help="disable stderr progress and human failure hints")
    parser.set_defaults(progress=None)
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    text_flags = (args.kind, args.project_id, args.task_id)
    if args.operation not in ("remember", "recall"):
        if args.input is not None or args.content_file is not None or any(value is not None for value in text_flags):
            raise BackendConfigError("only remember/recall accept business input")
    elif args.operation == "recall":
        if args.input is None or args.content_file is not None or any(value is not None for value in text_flags):
            raise BackendConfigError("recall requires --input and does not accept text-record flags")
    elif args.content_file is not None:
        if not all(isinstance(value, str) and value.strip() for value in text_flags):
            raise BackendConfigError("text submission requires --kind task_process, --project-id and --task-id")
    elif args.input is None or any(value is not None for value in text_flags):
        raise BackendConfigError("remember requires --input or a complete explicit text submission")


def _input_limit(config: dict) -> int:
    limit = config["options"].get("max_input_bytes", DEFAULT_MAX_INPUT_BYTES)
    if type(limit) is not int or not 0 < limit < sys.maxsize:
        raise BackendConfigError("options.max_input_bytes must be a positive supported integer byte limit")
    return limit


def _read_bytes(path: str, limit: int) -> bytes:
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(limit + 1)
        else:
            with open(path, "rb") as handle:
                raw = handle.read(limit + 1)
    except (OSError, ValueError, OverflowError):
        raise BackendConfigError("cannot read the explicitly supplied input file") from None
    if len(raw) > limit:
        raise BackendConfigError(f"input exceeds the configured {limit} UTF-8 byte limit", "TOO_LARGE")
    return raw


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackendConfigError("input JSON contains duplicate object keys")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise BackendConfigError("input JSON must not contain nonfinite numbers")


def _payload(args: argparse.Namespace, limit: int) -> dict | None:
    if args.operation not in ("remember", "recall"):
        return None
    path = args.content_file if args.content_file is not None else args.input
    raw = _read_bytes(path, limit)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise BackendConfigError("input must be strict UTF-8") from None
    if args.content_file is not None:
        return {"kind": "task_process", "project_id": args.project_id, "task_id": args.task_id, "content": text}
    try:
        result = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        result = clone_json(result)
    except (ValueError, RecursionError):
        raise BackendConfigError("input must be strict finite Unicode JSON without duplicate keys") from None
    if not isinstance(result, dict):
        raise BackendConfigError("business input must be a JSON object")
    return result


def create_backend(backend: str = "openviking") -> Any:
    """Import only for this namespace; httpx itself remains initialize-time lazy."""
    if backend == "tencentdb":
        from ..backends.tencentdb_http import TencentDBBackend
        return TencentDBBackend()
    if backend == "mem0":
        from ..backends.mem0_http import Mem0Backend
        return Mem0Backend()
    from ..backends.openviking_http import OpenVikingBackend
    return OpenVikingBackend()


async def _execute(config: dict, operation: str, payload: dict | None, timeout: float,
                   *, progress: _Progress | None = None) -> dict | None:
    progress = progress or _Progress(False)
    memory = NorthboundMemory(create_backend(config["backend"]))
    deadline = asyncio.get_running_loop().time() + timeout
    result: dict | None = None
    primary_produced = False
    close_reported = False

    async def run_stage(stage: str, call, *, bootstrap: bool = False):
        nonlocal result, primary_produced
        # Do not let a prior readiness result stand in for a cancelled data call.
        result, primary_produced = None, False

        async def capture():
            nonlocal result, primary_produced
            value = await call
            if bootstrap and value["ok"] and value["data"].get("state") != "ready":
                value = _error("UNAVAILABLE", "backend initialization did not reach ready")
            if not bootstrap or operation == "initialize" or not value["ok"]:
                # Capture before progress completion joins its ticker. A cancel
                # during that observational await cannot erase this outcome.
                result, primary_produced = value, True
            return value

        return await progress.run(stage, capture())

    try:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            result = _error("TIMEOUT", "command timed out before initialization")
        else:
            result = await run_stage("initialize", memory.initialize(config, timeout=remaining), bootstrap=True)
        if result["ok"]:
            if operation == "initialize":
                pass  # Return this probe's status, then release its client.
            elif operation == "close":
                close_reported = True
                result = await run_stage("close", memory.close())
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result = _error("TIMEOUT", "command timed out before the requested operation")
                elif operation == "status":
                    result = await run_stage("status", memory.status(timeout=remaining))
                elif operation == "remember":
                    result = await run_stage("remember", memory.remember(payload, timeout=remaining))
                else:
                    result = await run_stage("recall", memory.recall(payload, timeout=remaining))
    except asyncio.CancelledError:
        # Started writes are converted to WRITE_UNKNOWN inside the facade.
        if not primary_produced:
            result = _error("BACKEND_ERROR", "backend command was cancelled")
    except Exception:
        if not primary_produced:
            result = _error("BACKEND_ERROR", "backend command failed without a confirmed result")
    finally:
        cleanup, cleanup_produced = None, False

        async def capture_close():
            nonlocal cleanup, cleanup_produced
            cleanup = await memory.close()
            cleanup_produced = True
            return cleanup

        try:
            await progress.run("close cleanup" if close_reported else "close", capture_close())
        except asyncio.CancelledError:
            if not cleanup_produced:
                cleanup = _error("BACKEND_ERROR", "backend client cleanup was cancelled")
        except Exception:
            if not cleanup_produced:
                cleanup = _error("BACKEND_ERROR", "backend client cleanup failed")
        # Preserve primary failures, including a known uncertain-write source.
        if cleanup is not None and (result is None or result.get("ok") is True):
            # A confirmed save remains attributable even if releasing its client
            # failed. Never lose the source and invite an untraceable repeat.
            if (isinstance(result, dict) and isinstance(result.get("data"), dict)
                    and isinstance(result["data"].get("source_id"), str)):
                cleanup = clone_json(cleanup)
                cleanup["error"]["source_id"] = result["data"]["source_id"]
            result = cleanup
    return result


def _emit(result: dict | None) -> None:
    if result is None:
        result = {"ok": True, "data": None}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    enabled = sys.stderr.isatty()
    # Even parser failures can request safe hints; never echo rejected tokens.
    if "--no-progress" in arguments:
        enabled = False
    elif "--progress" in arguments:
        enabled = True
    progress = _Progress(enabled)
    backend = None
    try:
        args = build_parser().parse_args(arguments)
        progress.enabled = enabled if args.progress is None else args.progress
        _validate_arguments(args)
        config = load_config(args.config)
        backend = config["backend"]
        payload = _payload(args, _input_limit(config))
        result = asyncio.run(_execute(config, args.operation, payload, args.timeout, progress=progress))
    except BackendConfigError as exc:
        result = _error(exc.code, exc.message)
    except ImportError:
        result = _error("UNAVAILABLE", "memory backend adapter is unavailable")
    except KeyboardInterrupt:
        result = _error("BACKEND_ERROR", "backend command was interrupted")
    except Exception:
        result = _error("BACKEND_ERROR", "backend command failed without a valid result")
    progress.failure_hint(result, backend)
    _emit(result)
    return 0 if result is None or result.get("ok") is True else 1
