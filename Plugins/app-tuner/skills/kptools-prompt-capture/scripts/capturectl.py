"""File-based capture control; run in the proxy's PID/mount namespace."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from capture_runtime import _cap_atomic_json, capture_collect_state, capture_validate


def write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _cap_atomic_json(str(path), capture_validate(config))


def active_state(path: Path) -> dict:
    state = capture_collect_state(str(path))
    for key in ("workers", "writers"):
        state[key] = [s for s in state[key] if time.time() - s.get("updated", 0) < 10]
    return state


def wait_applied(path: Path, timeout: float) -> str:
    st = path.stat()
    sig = [st.st_ino, st.st_mtime_ns, st.st_size]
    deadline = time.monotonic() + timeout
    while True:
        workers = active_state(path)["workers"]
        if any(w.get("config_sig") == sig and w.get("config_error") for w in workers):
            return "rejected"
        if workers and all(w.get("config_sig") == sig for w in workers):
            return "ok"
        if time.monotonic() >= deadline:
            return "timeout" if workers else "no_workers"
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prompt capture control (no proxy restart)")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("cmd", choices=("init", "show", "status", "load", "set", "on", "off"))
    parser.add_argument("values", nargs="*")
    parser.add_argument("--duration", type=float, help="on: required capture window in seconds")
    parser.add_argument("--metadata-only", action="store_true",
                        help="on: refuse configuration that enables text, session following or golden")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout < 0:
        parser.error("--timeout must be finite and nonnegative")
    if args.metadata_only and args.cmd != "on":
        parser.error("--metadata-only is only valid with on")
    path = args.config.resolve()
    if args.cmd == "init":
        if path.exists():
            parser.error("config already exists; refusing to overwrite")
        write_config(path, {})
        return 0
    with path.open(encoding="utf-8") as stream:
        current = json.load(stream)
    if args.cmd == "show":
        print(json.dumps(current, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "status":
        state = active_state(path)
        state["note"] = "config.enabled is requested; workers[].enabled is effective (deadline may have expired)"
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["workers"] else 4
    patch = {}
    if args.cmd == "load":
        if len(args.values) != 1:
            parser.error("load needs one preset JSON path")
        with open(args.values[0], encoding="utf-8") as stream:
            patch = capture_validate(json.load(stream))
        # Presets cannot change the active window or its authorization.
        for key in ("out_dir", "enabled", "stop_at_unix"):
            patch[key] = current.get(key, patch[key])
        patch["metadata_only"] = current.get("metadata_only", False)
    elif args.cmd == "set":
        if not args.values:
            parser.error("set needs key=value")
        for value in args.values:
            if "=" not in value:
                parser.error("set needs key=value")
            key, raw = value.split("=", 1)
            if key in ("enabled", "stop_at_unix", "metadata_only"):
                parser.error("use on --duration or off instead")
            try:
                patch[key] = json.loads(raw)
            except ValueError:
                patch[key] = raw
    elif args.cmd == "on":
        if args.duration is None or not math.isfinite(args.duration) or args.duration <= 0:
            parser.error("on requires --duration > 0 (seconds)")
        now = time.time()
        prior = capture_validate(current)
        protected = prior["enabled"] and prior["metadata_only"] and prior["stop_at_unix"] > now
        patch = {"enabled": True, "stop_at_unix": now + args.duration,
                 "metadata_only": args.metadata_only or protected}
    elif args.cmd == "off":
        patch = {"enabled": False, "metadata_only": False}
    new = capture_validate({**current, **patch})
    if new["enabled"] and new["stop_at_unix"] <= time.time():
        parser.error("deadline expired; off first, configure, then on --duration")
    write_config(path, new)
    status = wait_applied(path, args.timeout)
    print(json.dumps({"config_path": str(path), "applied": status}, ensure_ascii=False))
    # In particular, never restore an enabled config when off was not acknowledged.
    return 0 if status == "ok" else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
