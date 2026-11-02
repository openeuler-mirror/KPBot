"""Explicit local configuration wizard; never connects to or starts a service."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import shlex
import sys
import warnings

from ..backend_config import BackendConfigError, validate_config
from ..backends.mem0_http import Mem0Backend, _Failure as Mem0Failure
from ..backends.openviking_http import OpenVikingBackend, _Failure as OpenVikingFailure
from ..backends.tencentdb_http import TencentDBBackend, _Failure as TencentFailure


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise BackendConfigError("invalid configure arguments; use configure --help")


def build_parser():
    parser = _Parser(prog="kpbot memory configure", description=__doc__)
    parser.add_argument("--backend", required=True, choices=("openviking", "mem0", "tencentdb"))
    parser.add_argument("--config", required=True, help="explicit new private configuration path; never overwritten")
    return parser


def _ask(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    sys.stderr.write(label + suffix + ": ")
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    value = line.strip()
    if not value and default is not None:
        return default
    if not value:
        raise BackendConfigError("required configuration input must not be empty")
    return value


def _collect(backend):
    port = {"openviking": 1933, "mem0": 8888, "tencentdb": 8420}[backend]
    endpoint = _ask("Service endpoint", f"http://127.0.0.1:{port}")
    owner = _ask("KPBot user ID (record owner)")
    current = _ask("Current project ID")
    projects = [part.strip() for part in _ask("Allowed project IDs (comma-separated)", current).split(",")]
    sys.stderr.write("Confirm the entered project access scope before saving.\n")
    if _ask("Confirm allowed projects? Enter yes to continue", "no").lower() != "yes":
        raise BackendConfigError("configuration cancelled; no file was created")
    options = {"endpoint": endpoint, "owner_id": owner}
    if backend == "openviking":
        options.update(account_id=_ask("OpenViking account ID"),
                       user_id=_ask("OpenViking provider user ID"), processing_mode="vectors_only")
    elif backend == "tencentdb":
        options.update(service_id=_ask("TencentDB service ID"), agent_id=_ask("TencentDB agent ID", "kpbot"))
    else:
        options["derive_memories"] = False
    with warnings.catch_warnings():
        # getpass must never fall back to echoing the secret on an unsuitable terminal.
        warnings.simplefilter("error", getpass.GetPassWarning)
        options["api_key"] = getpass.getpass("Service API key (required; hidden): ", stream=sys.stderr)
    if not options["api_key"].strip() or any(ord(c) < 32 or ord(c) == 127 for c in options["api_key"]):
        raise BackendConfigError("API key must be nonempty and contain no control characters")
    adapter = {"openviking": OpenVikingBackend, "mem0": Mem0Backend, "tencentdb": TencentDBBackend}[backend]
    try:
        options = adapter._normalize_options(options)
    except (OpenVikingFailure, Mem0Failure, TencentFailure) as exc:
        raise BackendConfigError(exc.message, exc.code) from None
    options.pop("owner_id", None)  # The common layer injects the explicit KPBot owner.
    return validate_config({"backend": backend, "user_id": owner, "current_project": current,
                            "allowed_projects": projects, "options": options})


def _write_new(path, config):
    """Create exactly one private file, removing our own partial file on failure."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
    payload = (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except OSError:
            pass
        raise


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise BackendConfigError("configure requires an interactive terminal for input and secret entry")
        path = Path(args.config).expanduser()
        if os.path.lexists(path):
            raise BackendConfigError("configuration path already exists; choose a new path", "ACCESS_DENIED")
        sys.stderr.write("Create a private client configuration only; no network or service changes.\n")
        config = _collect(args.backend)
        _write_new(path, config)
        result = {"ok": True, "data": {"config_path": str(path), "connection_verified": False}}
        sys.stderr.write("Configuration created. Connection has not been verified. Next run:\n"
                         "kpbot memory backend status --config " + shlex.quote(str(path)) + "\n")
    except (EOFError, KeyboardInterrupt):
        result = {"ok": False, "error": {"code": "INVALID_ARGUMENT",
                  "message": "configuration cancelled; no complete configuration was created", "source_id": None}}
    except getpass.GetPassWarning:
        result = {"ok": False, "error": {"code": "ACCESS_DENIED",
                  "message": "hidden secret input is unavailable; configuration was not created", "source_id": None}}
    except BackendConfigError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message, "source_id": None}}
    except OSError:
        result = {"ok": False, "error": {"code": "ACCESS_DENIED",
                  "message": "cannot create a new private configuration file", "source_id": None}}
    except Exception:
        result = {"ok": False, "error": {"code": "INVALID_ARGUMENT",
                  "message": "configuration could not be created from the supplied input", "source_id": None}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
    return 0 if result["ok"] else 1
