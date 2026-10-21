"""Explicit trusted configuration for the single-local-user memory backend.

This is deliberately separate from any bearer-token configuration. The
caller, not a business payload or a repository discovery heuristic, chooses
this file. It must belong to the effective OS user, be a regular non-symlink
file, and have mode 0600. Its owner is the trust boundary, not a tenant login.
No credentials or configuration values are included in error messages.
"""
from __future__ import annotations

import copy
import json
import math
import os
import stat
from typing import Any

MAX_CONFIG_BYTES = 1024 * 1024
CONFIG_FIELDS = frozenset(
    {"backend", "user_id", "current_project", "allowed_projects", "options"}
)
BACKEND_OPTIONS = {
    "openviking": frozenset(
        {"endpoint", "api_key", "account_id", "user_id", "owner_id",
         "processing_mode", "max_input_bytes"}
    ),
    "tencentdb": frozenset(
        {"endpoint", "api_key", "service_id", "agent_id", "owner_id",
         "max_input_bytes", "chunk_chars"}
    ),
}
_OPTION_STRINGS = frozenset(
    {"endpoint", "api_key", "account_id", "user_id", "service_id", "agent_id", "owner_id"}
)
_OPTION_INTEGERS = frozenset({"max_input_bytes", "chunk_chars"})
_OPENVIKING_MODES = frozenset({"vectors_only", "semantic_and_vectors"})

__all__ = ["BackendConfigError", "load_config", "validate_config"]


class BackendConfigError(ValueError):
    """A safe, stable configuration error suitable for a public envelope."""

    def __init__(self, message: str, code: str = "INVALID_ARGUMENT") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source_id = None


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def clone_json(value: Any) -> Any:
    """Validate ordinary JSON-compatible values and copy without normalising.

    Unlike a JSON round trip, this preserves integer/float values, dictionary
    order and every string exactly. Cycles, nonfinite numbers, non-string keys
    and invalid Unicode are rejected rather than converted or truncated.
    """
    ancestors: set[int] = set()

    def check(item: Any) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            item.encode("utf-8")
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("nonfinite number")
            return
        if not isinstance(item, (dict, list)):
            raise ValueError("not a JSON value")
        identity = id(item)
        if identity in ancestors:
            raise ValueError("cyclic value")
        ancestors.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("non-string key")
                    key.encode("utf-8")
                    check(child)
            else:
                for child in item:
                    check(child)
        finally:
            ancestors.remove(identity)

    try:
        check(value)
        return copy.deepcopy(value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BackendConfigError("value must be finite, valid Unicode JSON") from None


def _validate_options(backend: str, options: dict) -> None:
    """Check the per-backend option structure only; adapters revalidate semantics."""
    unknown = set(options) - BACKEND_OPTIONS[backend]
    if unknown:
        raise BackendConfigError(f"unknown {backend} backend option: {min(unknown)}")
    for name in _OPTION_STRINGS & options.keys():
        if not nonempty_string(options[name]):
            raise BackendConfigError(f"backend option {name} must be a non-empty string")
    for name in _OPTION_INTEGERS & options.keys():
        if type(options[name]) is not int or options[name] <= 0:
            raise BackendConfigError(f"backend option {name} must be a positive integer")
    if "processing_mode" in options and options["processing_mode"] not in _OPENVIKING_MODES:
        raise BackendConfigError(
            "backend option processing_mode must be vectors_only or semantic_and_vectors")


def validate_config(config: dict) -> dict:
    """Validate a trusted in-process configuration and return a private copy.

    Passing a dictionary does not authenticate it: only the application may
    supply this argument. Requests to remember/recall never set configuration.
    A new facade instance is required to change the accepted configuration.
    """
    if not isinstance(config, dict):
        raise BackendConfigError("backend configuration must be an object")
    if set(config) != CONFIG_FIELDS:
        raise BackendConfigError("backend configuration requires exactly the five documented fields")
    if config["backend"] not in BACKEND_OPTIONS:
        raise BackendConfigError("only the openviking and tencentdb backends are supported", "UNSUPPORTED")
    for field in ("user_id", "current_project"):
        if not nonempty_string(config[field]):
            raise BackendConfigError(f"configuration {field} must be a non-empty string")
    projects = config["allowed_projects"]
    if (
        not isinstance(projects, list)
        or not projects
        or any(not nonempty_string(project) for project in projects)
    ):
        raise BackendConfigError("allowed_projects must be a non-empty list of project IDs")
    if len(set(projects)) != len(projects):
        raise BackendConfigError("allowed_projects must not contain duplicate project IDs")
    if config["current_project"] not in projects:
        raise BackendConfigError("current_project must belong to allowed_projects", "ACCESS_DENIED")
    if not isinstance(config["options"], dict):
        raise BackendConfigError("backend options must be an object")
    _validate_options(config["backend"], config["options"])
    return clone_json(config)


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackendConfigError("configuration JSON contains duplicate keys")
        result[key] = value
    return result


def _constant(_name: str) -> None:
    raise BackendConfigError("configuration JSON must not contain nonfinite numbers")


def load_config(path: os.PathLike | str) -> dict:
    """Read an explicitly chosen owner-owned 0600 file, without following links.

    The opened descriptor is checked again to avoid trusting a different file
    exchanged between lstat and open. The byte limit is 1 MiB; no partial input
    is ever accepted. Unknown fields are rejected.
    """
    try:
        filename = os.fspath(path)
        before = os.lstat(filename)
    except (OSError, TypeError, ValueError):
        raise BackendConfigError("cannot inspect the backend configuration file") from None
    if not stat.S_ISREG(before.st_mode):
        raise BackendConfigError("backend configuration must be a regular non-symlink file", "ACCESS_DENIED")
    if stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.geteuid():
        raise BackendConfigError("backend configuration must be owned by the current OS user with mode 0600", "ACCESS_DENIED")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(filename, flags)
    except OSError:
        raise BackendConfigError("cannot open the backend configuration file", "ACCESS_DENIED") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise BackendConfigError("backend configuration ownership, mode or file identity changed", "ACCESS_DENIED")
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise BackendConfigError("cannot read the backend configuration file") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise BackendConfigError("backend configuration exceeds the 1048576 byte limit", "TOO_LARGE")
    try:
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise BackendConfigError("backend configuration must be strict UTF-8 JSON") from None
    return validate_config(config)
