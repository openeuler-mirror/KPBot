"""One-shot OpenViking backend CLI, separate from the call/serve/demo commands.

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
from typing import Any

from .backend_config import BackendConfigError, clone_json, load_config
from .northbound import NorthboundMemory

DEFAULT_MAX_INPUT_BYTES = 1_048_576
OPERATIONS = ("initialize", "status", "remember", "recall", "close")

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
            "One-shot OpenViking client: initialize, perform one operation, then close. "
            "CLI invocations have no cross-process state; initialize/status are fresh "
            "readiness probes, close releases only this invocation's client, not the service."
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


def create_backend() -> Any:
    """Import only for this namespace; httpx itself remains initialize-time lazy."""
    from .backends.openviking_http import OpenVikingBackend
    return OpenVikingBackend()


async def _execute(config: dict, operation: str, payload: dict | None, timeout: float) -> dict | None:
    memory = NorthboundMemory(create_backend())
    deadline = asyncio.get_running_loop().time() + timeout
    result: dict | None = None
    try:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            result = _error("TIMEOUT", "command timed out before initialization")
        else:
            result = await memory.initialize(config, timeout=remaining)
        if result["ok"]:
            if result["data"].get("state") != "ready":
                result = _error("UNAVAILABLE", "backend initialization did not reach ready")
            elif operation == "initialize":
                pass  # Return this probe's status, then release its client.
            elif operation == "close":
                result = await memory.close()
            else:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result = _error("TIMEOUT", "command timed out before the requested operation")
                elif operation == "status":
                    result = await memory.status(timeout=remaining)
                elif operation == "remember":
                    result = await memory.remember(payload, timeout=remaining)
                else:
                    result = await memory.recall(payload, timeout=remaining)
    except asyncio.CancelledError:
        # Started writes are converted to WRITE_UNKNOWN inside the facade.
        result = _error("BACKEND_ERROR", "backend command was cancelled")
    except Exception:
        result = _error("BACKEND_ERROR", "backend command failed without a confirmed result")
    finally:
        try:
            cleanup = await memory.close()
        except asyncio.CancelledError:
            cleanup = _error("BACKEND_ERROR", "backend client cleanup was cancelled")
        except Exception:
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
    try:
        args = build_parser().parse_args(argv)
        _validate_arguments(args)
        config = load_config(args.config)
        payload = _payload(args, _input_limit(config))
        result = asyncio.run(_execute(config, args.operation, payload, args.timeout))
    except BackendConfigError as exc:
        result = _error(exc.code, exc.message)
    except ImportError:
        result = _error("UNAVAILABLE", "OpenViking client adapter is unavailable")
    except KeyboardInterrupt:
        result = _error("BACKEND_ERROR", "backend command was interrupted")
    except Exception:
        result = _error("BACKEND_ERROR", "backend command failed without a valid result")
    _emit(result)
    return 0 if result is None or result.get("ok") is True else 1
