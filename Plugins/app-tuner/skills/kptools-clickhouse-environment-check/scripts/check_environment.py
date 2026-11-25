#!/usr/bin/env python3
"""Run bounded, read-only ClickHouse benchmark prerequisite probes."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

MIN_SERVER_VERSION = (22, 8, 0)
MIN_OUTPUT_FREE_BYTES = 1024 * 1024
VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){2,3})(?!\d)")
AUTH_MARKERS = (
    "AUTHENTICATION_FAILED",
    "WRONG_PASSWORD",
    "REQUIRED_PASSWORD",
    "ACCESS_DENIED",
    "UNAUTHORIZED",
    "Code: 516",
)


def finite_timeout(value):
    """Parse a finite per-process timeout suitable for short probes."""
    seconds = float(value)
    if not math.isfinite(seconds) or not 0.05 <= seconds <= 600:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and between 0.05 and 600 seconds"
        )
    return seconds


def network_port(value):
    """Parse a TCP port without allowing zero or out-of-range values."""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def secure_config(value):
    """Accept only a private regular file that may contain credentials."""
    path = Path(value)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise argparse.ArgumentTypeError(
            f"configuration is not readable: {exc.strerror or exc}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise argparse.ArgumentTypeError(
            "configuration must be a regular file"
        )
    if stat.S_IMODE(mode) & 0o077:
        raise argparse.ArgumentTypeError(
            "configuration permissions must not grant group/other access"
        )
    return path


def build_parser():
    """Build the command-line parser; no password argument is provided."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes: 0=ready; 1=blocked; "
            "2=argument/output error or collision; 3=unknown"
        ),
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=network_port, default=9000)
    parser.add_argument(
        "--user",
        default=None,
        help=(
            "explicit client user; when omitted the --config value or the "
            "client built-in default (default) is used"
        ),
    )
    parser.add_argument("--client", default="clickhouse-client")
    parser.add_argument("--benchmark", default="clickhouse-benchmark")
    parser.add_argument(
        "--single-binary",
        action="store_true",
        help="use --client for both 'client' and 'benchmark' subcommands",
    )
    parser.add_argument("--config", type=secure_config)
    parser.add_argument("--secure", action="store_true")
    parser.add_argument("--timeout", type=finite_timeout, default=20.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def redact(text):
    """Redact the injected password and common password-shaped diagnostics."""
    redacted = text or ""
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    if password:
        redacted = redacted.replace(password, "[REDACTED]")
    return re.sub(
        r"(?i)(password[\"']?\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )


def child_environment():
    """Pass only variables needed by the official client and TLS setup."""
    names = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_QUOTA_KEY",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def stop_process_group(process):
    """Kill only the owned group, including descendants after leader exit."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_process(argv, timeout):
    """Run one bounded subprocess and return sanitized captured evidence."""
    started = time.monotonic_ns()
    outcome = {
        "returncode": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "error": None,
    }
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=child_environment(),
            start_new_session=True,
        )
    except OSError as exc:
        outcome["error"] = exc
    else:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome["timed_out"] = True
            stop_process_group(process)
            stdout, stderr = process.communicate(timeout=2)
        except BaseException:
            stop_process_group(process)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise
        outcome["returncode"] = process.returncode
        outcome["stdout"] = redact(stdout)
        outcome["stderr"] = redact(stderr)
    outcome["duration_ms"] = round(
        (time.monotonic_ns() - started) / 1_000_000, 3
    )
    return outcome


def private_write(path, content):
    """Create a new private evidence file without following an existing one."""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def classify_error(stderr):
    """Separate authentication failures from other client failures."""
    upper = (stderr or "").upper()
    if any(marker.upper() in upper for marker in AUTH_MARKERS):
        return "auth"
    return "error"


def process_check(name, argv, timeout, output, sql=None):
    """Execute a probe and convert process outcomes to status vocabulary."""
    evidence = {"timeout_seconds": timeout}
    if sql is not None:
        evidence["sql"] = sql
    outcome = run_process(argv, timeout)
    stem = name.replace("_", "-")
    private_write(output / f"{stem}.stdout.log", outcome["stdout"])
    private_write(output / f"{stem}.stderr.log", outcome["stderr"])
    evidence.update(
        {
            "returncode": outcome["returncode"],
            "duration_ms": outcome["duration_ms"],
            "stdout_log": f"{stem}.stdout.log",
            "stderr_log": f"{stem}.stderr.log",
        }
    )
    check = {
        "name": name,
        "mandatory": True,
        "status": "ready",
        "reason": None,
        "evidence": evidence,
    }
    if outcome["error"] is not None:
        check["status"] = "blocked"
        check["reason"] = (
            "missing_tool"
            if isinstance(outcome["error"], FileNotFoundError)
            else "error"
        )
        evidence["message"] = redact(str(outcome["error"]))
    elif outcome["timed_out"]:
        check["status"] = "blocked"
        check["reason"] = "timeout"
    elif outcome["returncode"] != 0:
        check["status"] = "blocked"
        check["reason"] = classify_error(outcome["stderr"])
        evidence["message"] = outcome["stderr"][-2000:]
    return check, outcome


def version_tuple(text):
    """Preserve all three or four numeric components of a CLI version."""
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def mark(check, status, reason=None, **evidence):
    """Update a probe status and attach non-secret structured evidence."""
    check["status"] = status
    check["reason"] = reason
    check["evidence"].update(evidence)
    return check


def dependency_check(name, dependency):
    """Record a check that cannot run after a failed prerequisite."""
    return {
        "name": name,
        "mandatory": True,
        "status": "unknown",
        "reason": "dependency_failed",
        "evidence": {"dependency": dependency},
    }


def command_prefix(args, benchmark=False):
    """Construct a fixed argv prefix without a shell or password option."""
    if args.single_binary:
        argv = [args.client, "benchmark" if benchmark else "client"]
    else:
        argv = [args.benchmark if benchmark else args.client]
    if args.config:
        option = "--config" if benchmark else "--config-file"
        argv += [option, str(args.config)]
    return argv


def client_version_command(args):
    """Read a binary version without the unsupported benchmark flag."""
    if args.single_binary:
        return [args.client, "--version"]
    return command_prefix(args) + ["--version"]


def connection_arguments(args):
    """Return explicit non-secret target options shared by live probes."""
    argv = [
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.user is not None:
        argv += ["--user", args.user]
    argv += [
        "--connect_timeout",
        str(max(1, math.ceil(args.timeout))),
        "--receive_timeout",
        str(max(1, math.ceil(args.timeout))),
    ]
    if args.secure:
        argv.append("--secure")
    return argv


def run_checks(args):
    """Run only probes required by the bundled synthetic SELECT benchmark."""
    output = args.output
    checks = []
    storage = {
        "name": "output_storage",
        "mandatory": True,
        "status": "unknown",
        "reason": None,
        "evidence": {"minimum_free_bytes": MIN_OUTPUT_FREE_BYTES},
    }
    try:
        free_bytes = shutil_disk_usage(output)
    except OSError as exc:
        mark(storage, "blocked", "error", message=redact(str(exc)))
    else:
        storage["evidence"]["free_bytes"] = free_bytes
        if free_bytes < MIN_OUTPUT_FREE_BYTES:
            mark(storage, "blocked", "resource")
        else:
            mark(storage, "ready")
    checks.append(storage)

    client_check, client_outcome = process_check(
        "client_tool",
        client_version_command(args),
        args.timeout,
        output,
    )
    client_version = None
    if client_check["status"] == "ready":
        client_version = version_tuple(client_outcome["stdout"])
        if client_version is None:
            mark(client_check, "unknown", "invalid_version")
        else:
            client_check["evidence"]["version"] = ".".join(
                map(str, client_version)
            )
    checks.append(client_check)

    interface_check, interface_outcome = process_check(
        "benchmark_interface",
        command_prefix(args, benchmark=True) + ["--help"],
        args.timeout,
        output,
    )
    if interface_check["status"] == "ready":
        help_text = interface_outcome["stdout"] + interface_outcome["stderr"]
        required = (
            "--query",
            "--iterations",
            "--concurrency",
            "--delay",
            "--client-side-time",
        )
        missing = [flag for flag in required if flag not in help_text]
        if args.config and "--config" not in help_text:
            missing.append("--config")
        if missing:
            mark(
                interface_check,
                "blocked",
                "wrong_version",
                missing_flags=missing,
            )
        else:
            interface_check["evidence"]["required_flags"] = list(required)
            interface_check["evidence"]["version_probe"] = (
                "not invoked; benchmark --version is not portable"
            )
            if args.single_binary and client_version is not None:
                interface_check["evidence"]["version"] = ".".join(
                    map(str, client_version)
                )
                interface_check["evidence"]["version_source"] = (
                    "shared single binary"
                )
    checks.append(interface_check)

    server_check = None
    server_version = None
    if client_check["status"] != "ready":
        server_check = dependency_check("server_interface", "client_tool")
        server_outcome = None
    else:
        sql = "SELECT version()"
        server_check, server_outcome = process_check(
            "server_interface",
            command_prefix(args)
            + connection_arguments(args)
            + ["--format", "TSV", "--query", sql],
            args.timeout,
            output,
            sql,
        )
        if server_check["status"] == "ready":
            raw_version = server_outcome["stdout"].strip()
            server_version = version_tuple(raw_version)
            server_check["evidence"]["version_output"] = raw_version
            if server_version is None:
                mark(server_check, "unknown", "invalid_version")
            elif server_version < MIN_SERVER_VERSION:
                mark(
                    server_check,
                    "blocked",
                    "wrong_version",
                    minimum_version=".".join(map(str, MIN_SERVER_VERSION)),
                )
            else:
                server_check["evidence"]["parsed_version"] = server_version
    checks.append(server_check)

    if server_check["status"] != "ready":
        correctness = dependency_check("exact_query", "server_interface")
    else:
        sql = "SELECT sum(number) FROM numbers(1000)"
        correctness, correctness_outcome = process_check(
            "exact_query",
            command_prefix(args)
            + connection_arguments(args)
            + ["--format", "TSV", "--query", sql],
            args.timeout,
            output,
            sql,
        )
        if correctness["status"] == "ready":
            actual = correctness_outcome["stdout"].strip()
            correctness["evidence"]["expected"] = "499500"
            correctness["evidence"]["actual"] = actual
            if actual != "499500":
                mark(correctness, "blocked", "correctness")
    checks.append(correctness)

    series = {
        "name": "client_server_series",
        "mandatory": False,
        "status": "unknown",
        "reason": "insufficient_version_evidence",
        "evidence": {},
    }
    if client_version is not None and server_version is not None:
        series["evidence"] = {
            "client_series": list(client_version[:2]),
            "server_series": list(server_version[:2]),
        }
        if client_version[:2] == server_version[:2]:
            mark(series, "ready")
        else:
            mark(series, "unknown", "version_mismatch")
    checks.append(series)

    mandatory = [check for check in checks if check["mandatory"]]
    if any(check["status"] == "blocked" for check in mandatory):
        status = "blocked"
    elif any(check["status"] == "unknown" for check in mandatory):
        status = "unknown"
    else:
        status = "ready"
    counts = {
        state: sum(check["status"] == state for check in checks)
        for state in ("ready", "blocked", "unknown")
    }
    return {
        "schema_version": "kptools.clickhouse.environment.v1",
        "service": "clickhouse",
        "status": status,
        "ready": status == "ready",
        "scope": (
            "existing-target synthetic SELECT benchmark prerequisites only"
        ),
        "target": {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "user_source": (
                "cli"
                if args.user is not None
                else "config"
                if args.config is not None
                else "client_default"
            ),
            "secure": args.secure,
            "config_provided": args.config is not None,
            "single_binary": args.single_binary,
        },
        "checks": checks,
        "summary": counts,
        "limitations": [
            "No service, OS, schema, or configuration is changed.",
            (
                "Ready is not a production-health, cluster, replica, or "
                "capacity claim."
            ),
            (
                "Compatibility requires these live probes; version text "
                "alone is insufficient."
            ),
        ],
    }


def shutil_disk_usage(path):
    """Isolate disk measurement for focused contract tests."""
    import shutil

    return shutil.disk_usage(path).free


def main(argv=None):
    """Create fresh evidence, run probes, and return a meaningful exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host or "\x00" in args.host:
        parser.error("host must be non-empty and contain no NUL")
    if args.user is not None and ("\x00" in args.user or not args.user):
        parser.error("--user must be non-empty and contain no NUL")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        print(f"cannot create fresh output directory: {exc}", file=sys.stderr)
        return 2
    result = run_checks(args)
    try:
        private_write(
            args.output / "result.json",
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as exc:
        print(f"cannot write result: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "ready":
        return 0
    return 1 if result["status"] == "blocked" else 3


if __name__ == "__main__":
    sys.exit(main())
