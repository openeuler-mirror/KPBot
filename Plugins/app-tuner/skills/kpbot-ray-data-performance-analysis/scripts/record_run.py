#!/usr/bin/env python3
"""Record one foreground command without shell expansion or output parsing."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time


def private_binary(path):
    """Create an owner-only binary file, ignoring a permissive umask."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


def private_text(path):
    """Create an owner-only UTF-8 text file, ignoring a permissive umask."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def stop_process(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        if process.poll() is None:
            process.kill()
    process.wait(timeout=15)


def record(command, directory, timeout):
    if not command:
        raise ValueError("command must not be empty")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    result = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "cwd": str(Path.cwd()),
        "timing_scope": "popen_start_to_process_exit",
        "timeout_seconds": timeout,
        "elapsed_seconds": None,
        "returncode": None,
        "timed_out": False,
        "interrupted": False,
        "success": False,
        "correctness": "unknown",
        "error": None,
    }
    start = None
    process = None
    try:
        with (
            private_binary(directory / "stdout.log") as stdout,
            private_binary(directory / "stderr.log") as stderr,
        ):
            # Start after log creation so slow evidence storage does not
            # inflate the measured command time.
            start = time.perf_counter()
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name == "posix",
            )
            try:
                result["returncode"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                stop_process(process)
                result["returncode"] = process.returncode
            result["elapsed_seconds"] = time.perf_counter() - start
    except KeyboardInterrupt:
        result["interrupted"] = True
        if process is not None:
            stop_process(process)
            result["returncode"] = process.returncode
        if start is not None:
            result["elapsed_seconds"] = time.perf_counter() - start
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        if process is not None and process.poll() is None:
            stop_process(process)
        if start is not None:
            result["elapsed_seconds"] = time.perf_counter() - start
    finally:
        result["success"] = (
            result["returncode"] == 0
            and not result["timed_out"]
            and not result["interrupted"]
            and result["error"] is None
        )
        # JSON finalization happens after the timer stops.
        with private_text(directory / "run.json") as output:
            json.dump(
                result, output, ensure_ascii=False, indent=2, allow_nan=False
            )
            output.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", required=True, type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error(
            "Provide a command after -- and a finite positive --timeout."
        )
    try:
        result = record(command, args.out, args.timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Cannot record run: {error}\n")
    code = (
        124
        if result["timed_out"]
        else 130
        if result["interrupted"]
        else (0 if result["success"] else 1)
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
