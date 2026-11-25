#!/usr/bin/env python3
"""Run a bounded, read-only synthetic ClickHouse SELECT benchmark."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

MIN_SERVER_VERSION = (22, 8, 0)
VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){2,3})(?!\d)")
NUMBER = r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
SUMMARY_RE = re.compile(
    r",\s*queries:?\s+(\d+),\s*QPS:\s*"
    + NUMBER
    + r",\s*RPS:\s*"
    + NUMBER
    + r",\s*MiB/s:\s*"
    + NUMBER
    + r",\s*result RPS:\s*"
    + NUMBER
    + r",\s*result MiB/s:\s*"
    + NUMBER
)
EXECUTED_RE = re.compile(r"Queries executed:\s*(\d+)")
LATENCY_RE = re.compile(
    r"(?m)^\s*(50(?:\.0+)?|95(?:\.0+)?|99(?:\.0+)?)%\s+" + NUMBER + r"\s+sec\."
)
AUTH_MARKERS = (
    "AUTHENTICATION_FAILED",
    "WRONG_PASSWORD",
    "REQUIRED_PASSWORD",
    "ACCESS_DENIED",
    "UNAUTHORIZED",
    "Code: 516",
)


class RunFailure(Exception):
    """A classified failure that must be preserved in the JSON result."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def finite_timeout(value):
    """Parse a finite per-process timeout; unbounded values are rejected."""
    seconds = float(value)
    if not math.isfinite(seconds) or not 0.05 <= seconds <= 600:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and between 0.05 and 600 seconds"
        )
    return seconds


def bounded_int(minimum, maximum):
    """Build an integer parser with an explicit inclusive resource bound."""

    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return number

    return parse


def network_port(value):
    """Parse a valid non-zero TCP port."""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def secure_config(value):
    """Accept a private regular file because it may contain credentials."""
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


def configured_user(path):
    """Read only the configured user; never return or persist other values."""
    if path is None:
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"cannot parse client configuration: {exc}") from exc
    user = root.findtext("user") if root.tag == "clickhouse" else None
    if user is None and root.tag != "clickhouse":
        user = root.findtext("clickhouse/user")
    if user is None:
        return None
    user = user.strip()
    if not user or "\x00" in user:
        raise ValueError(
            "configured client user must be non-empty and contain no NUL"
        )
    return user


def resolve_user(args):
    """Resolve and explicitly share the same account across client tools."""
    if args.user is not None:
        args.resolved_user = args.user
        args.user_source = "cli"
        return
    config_user = configured_user(args.config)
    if config_user is not None:
        args.resolved_user = config_user
        args.user_source = "config"
    else:
        args.resolved_user = "default"
        args.user_source = "client_default"


def build_parser():
    """Build a bounded interface without arbitrary SQL or passwords."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes: 0=success; 1=preflight/workload failure; "
            "2=argument/output error or collision; 3=unknown (reserved); "
            "130=interrupted"
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
    parser.add_argument("--timeout", type=finite_timeout, default=120.0)
    parser.add_argument("--rounds", type=bounded_int(3, 10), default=3)
    parser.add_argument("--iterations", type=bounded_int(1, 10000), default=10)
    parser.add_argument("--warmup-iterations", type=bounded_int(1, 10000))
    parser.add_argument("--concurrency", type=bounded_int(1, 16), default=1)
    parser.add_argument(
        "--rows", type=bounded_int(1000, 1000000), default=100000
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def redact(text):
    """Redact the injected password and password-shaped diagnostic values."""
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
    """Pass only variables needed by the official clients and TLS setup."""
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
    """Capture raw output for semantics and redacted copies for evidence."""
    started = time.monotonic_ns()
    outcome = {
        "returncode": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "raw_stdout": "",
        "raw_stderr": "",
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
        outcome["raw_stdout"] = stdout
        outcome["raw_stderr"] = stderr
        outcome["stdout"] = redact(stdout)
        outcome["stderr"] = redact(stderr)
    outcome["duration_ms"] = round(
        (time.monotonic_ns() - started) / 1_000_000, 3
    )
    return outcome


def private_write(path, content):
    """Exclusively create a private result or sanitized log file."""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def classify_error(stderr):
    """Classify authentication separately from other client failures."""
    upper = (stderr or "").upper()
    if any(marker.upper() in upper for marker in AUTH_MARKERS):
        return "auth"
    return "error"


def failure_kind(outcome, missing_tool=True):
    """Map a process outcome to stable failure categories."""
    if outcome["error"] is not None:
        if missing_tool and isinstance(outcome["error"], FileNotFoundError):
            return "missing_tool"
        return "error"
    if outcome["timed_out"]:
        return "timeout"
    if outcome["returncode"]:
        return classify_error(outcome["raw_stderr"])
    return None


def outcome_message(outcome):
    """Select bounded non-secret diagnostic text for JSON evidence."""
    if outcome["error"] is not None:
        return redact(str(outcome["error"]))[:2000]
    if outcome["timed_out"]:
        return "subprocess exceeded its finite timeout"
    return (outcome["stderr"] or outcome["stdout"])[-2000:]


def command_prefix(args, benchmark=False):
    """Build the official CLI prefix without a shell or password argument."""
    if args.single_binary:
        argv = [args.client, "benchmark" if benchmark else "client"]
    else:
        argv = [args.benchmark if benchmark else args.client]
    if args.config:
        option = "--config" if benchmark else "--config-file"
        argv += [option, str(args.config)]
    return argv


def client_version_command(args):
    """Read the shared binary version, never 'benchmark --version'."""
    if args.single_binary:
        return [args.client, "--version"]
    return command_prefix(args) + ["--version"]


def connection_arguments(args):
    """Return explicit target arguments shared by validation and load."""
    argv = [
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    argv += ["--user", args.resolved_user]
    argv += [
        "--connect_timeout",
        str(max(1, math.ceil(args.timeout))),
        "--receive_timeout",
        str(max(1, math.ceil(args.timeout))),
    ]
    if args.secure:
        argv.append("--secure")
    return argv


def version_tuple(text):
    """Preserve all three or four numeric components for exact evidence."""
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def record_probe(result, name, argv, args, sql=None):
    """Run and retain one preflight probe, raising on any failed dependency."""
    outcome = run_process(argv, args.timeout)
    stem = f"preflight-{name.replace('_', '-')}"
    private_write(
        result["output_dir"] / f"{stem}.stdout.log", outcome["stdout"]
    )
    private_write(
        result["output_dir"] / f"{stem}.stderr.log", outcome["stderr"]
    )
    kind = failure_kind(outcome)
    evidence = {
        "returncode": outcome["returncode"],
        "duration_ms": outcome["duration_ms"],
        "timeout_seconds": args.timeout,
        "stdout_log": f"{stem}.stdout.log",
        "stderr_log": f"{stem}.stderr.log",
    }
    if sql is not None:
        evidence["sql"] = sql
    check = {
        "name": name,
        "mandatory": True,
        "status": "ready" if kind is None else "blocked",
        "reason": kind,
        "evidence": evidence,
    }
    result["preflight"]["checks"].append(check)
    if kind is not None:
        evidence["message"] = outcome_message(outcome)
        raise RunFailure(kind, f"preflight probe failed: {name}")
    return outcome, check


def preflight(args, result):
    """Recheck live tools, exact SQL behavior, and version/interfaces now."""
    client_outcome, client_check = record_probe(
        result,
        "client_tool",
        client_version_command(args),
        args,
    )
    client_version = version_tuple(client_outcome["raw_stdout"])
    if client_version is None:
        client_check["status"] = "unknown"
        client_check["reason"] = "invalid_version"
        raise RunFailure("wrong_version", "cannot parse client version")
    client_check["evidence"]["version"] = ".".join(map(str, client_version))
    result["environment"]["client_version"] = client_check["evidence"][
        "version"
    ]

    interface_outcome, interface_check = record_probe(
        result,
        "benchmark_interface",
        command_prefix(args, benchmark=True) + ["--help"],
        args,
    )
    help_text = (
        interface_outcome["raw_stdout"] + interface_outcome["raw_stderr"]
    )
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
        interface_check["status"] = "blocked"
        interface_check["reason"] = "wrong_version"
        interface_check["evidence"]["missing_flags"] = missing
        raise RunFailure("wrong_version", "benchmark lacks required interface")
    interface_check["evidence"]["required_flags"] = list(required)
    interface_check["evidence"]["version_probe"] = (
        "not invoked; benchmark --version is not portable"
    )
    if args.single_binary:
        interface_check["evidence"]["version"] = ".".join(
            map(str, client_version)
        )
        interface_check["evidence"]["version_source"] = "shared single binary"
        result["environment"]["benchmark_version"] = ".".join(
            map(str, client_version)
        )
    else:
        result["environment"]["benchmark_version"] = "unavailable"
        result["environment"]["benchmark_version_evidence"] = (
            "standalone benchmark exposes no portable version command"
        )

    server_outcome, server_check = record_probe(
        result,
        "server_interface",
        command_prefix(args)
        + connection_arguments(args)
        + ["--format", "TSV", "--query", "SELECT version()"],
        args,
        "SELECT version()",
    )
    raw_version = server_outcome["raw_stdout"].strip()
    server_version = version_tuple(raw_version)
    server_check["evidence"]["version_output"] = redact(raw_version)
    if server_version is None:
        server_check["status"] = "unknown"
        server_check["reason"] = "invalid_version"
        raise RunFailure("wrong_version", "cannot parse server version")
    if server_version < MIN_SERVER_VERSION:
        server_check["status"] = "blocked"
        server_check["reason"] = "wrong_version"
        server_check["evidence"]["minimum_version"] = ".".join(
            map(str, MIN_SERVER_VERSION)
        )
        raise RunFailure(
            "wrong_version", "server predates the bounded contract"
        )
    server_check["evidence"]["parsed_version"] = server_version
    result["environment"]["server_version"] = redact(raw_version)

    sql = "SELECT sum(number) FROM numbers(1000)"
    exact_outcome, exact_check = record_probe(
        result,
        "exact_query",
        command_prefix(args)
        + connection_arguments(args)
        + ["--format", "TSV", "--query", sql],
        args,
        sql,
    )
    actual = exact_outcome["raw_stdout"].strip()
    exact_check["evidence"].update(
        {"expected": "499500", "actual": redact(actual)}
    )
    if actual != "499500":
        exact_check["status"] = "blocked"
        exact_check["reason"] = "correctness"
        raise RunFailure("correctness", "preflight aggregate is not exact")
    result["preflight"]["status"] = "ready"


def parse_native_metrics(text, expected_queries):
    """Parse one final official report without inferring missing values."""
    summaries = list(SUMMARY_RE.finditer(text))
    executed = EXECUTED_RE.findall(text)
    if not summaries or not executed:
        raise ValueError("native query count or throughput report is missing")
    summary = summaries[-1]
    query_count = int(summary.group(1))
    executed_count = int(executed[-1])
    if query_count != expected_queries or executed_count != expected_queries:
        raise ValueError(
            "native query counts do not match the bounded iteration request"
        )
    values = [float(summary.group(index)) for index in range(2, 7)]
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError("native throughput contains a non-finite value")
    latencies = {}
    for match in LATENCY_RE.finditer(text):
        percentile = {50.0: "p50", 95.0: "p95", 99.0: "p99"}[
            float(match.group(1))
        ]
        seconds = float(match.group(2))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("native latency contains an invalid value")
        latencies[percentile] = seconds * 1000
    if set(latencies) != {"p50", "p95", "p99"}:
        raise ValueError("native p50/p95/p99 latency report is incomplete")
    return {
        "query_count": query_count,
        "qps": values[0],
        "read_rows_per_second": values[1],
        "read_mib_per_second": values[2],
        "result_rows_per_second": values[3],
        "result_mib_per_second": values[4],
        "latency_ms": latencies,
        "latency_scope": "client-side query execution time",
    }


def workload_query(rows):
    """Generate one fixed SELECT-only statement from a bounded integer."""
    return f"SELECT sum(number) FROM numbers({rows})"


def validate_workload(args, result, phase, query, expected):
    """Validate the exact aggregate outside the native timing interval."""
    outcome = run_process(
        command_prefix(args)
        + connection_arguments(args)
        + ["--format", "TSV", "--query", query],
        args.timeout,
    )
    stem = f"{phase}.validation"
    private_write(
        result["output_dir"] / f"{stem}.stdout.log", outcome["stdout"]
    )
    private_write(
        result["output_dir"] / f"{stem}.stderr.log", outcome["stderr"]
    )
    kind = failure_kind(outcome)
    actual = outcome["raw_stdout"].strip()
    validation = {
        "expected": str(expected),
        "actual": redact(actual),
        "returncode": outcome["returncode"],
        "duration_ms": outcome["duration_ms"],
        "passed": kind is None and actual == str(expected),
        "reason": kind,
        "stdout_log": f"{stem}.stdout.log",
        "stderr_log": f"{stem}.stderr.log",
    }
    if kind is None and actual != str(expected):
        validation["reason"] = "correctness"
    return validation


def run_phase(args, result, phase, iterations):
    """Run one native workload and its separate exact correctness check."""
    query = result["workload"]["query"]
    expected = result["workload"]["expected_aggregate"]
    argv = (
        command_prefix(args, benchmark=True)
        + connection_arguments(args)
        + [
            "--query",
            query,
            "--iterations",
            str(iterations),
            "--concurrency",
            str(args.concurrency),
            "--delay",
            "0",
            "--client-side-time",
        ]
    )
    outcome = run_process(argv, args.timeout)
    private_write(
        result["output_dir"] / f"{phase}.stdout.log", outcome["stdout"]
    )
    private_write(
        result["output_dir"] / f"{phase}.stderr.log", outcome["stderr"]
    )
    record = {
        "phase": phase,
        "included_in_summary": phase != "warmup",
        "status": "passed",
        "returncode": outcome["returncode"],
        "timed_out": outcome["timed_out"],
        "wall_time_ms": outcome["duration_ms"],
        "timeout_seconds": args.timeout,
        "stdout_log": f"{phase}.stdout.log",
        "stderr_log": f"{phase}.stderr.log",
        "native": None,
        "correctness": None,
        "failure": None,
    }
    kind = failure_kind(outcome)
    parse_failure = None
    if kind is None:
        try:
            record["native"] = parse_native_metrics(
                outcome["raw_stdout"] + "\n" + outcome["raw_stderr"],
                iterations,
            )
        except ValueError as exc:
            parse_failure = str(exc)
    else:
        record["failure"] = {"kind": kind, "message": outcome_message(outcome)}

    if kind is None:
        record["correctness"] = validate_workload(
            args, result, phase, query, expected
        )
        if not record["correctness"]["passed"]:
            record["failure"] = {
                "kind": record["correctness"]["reason"] or "correctness",
                "message": "exact workload validation failed",
            }
        elif parse_failure is not None:
            record["failure"] = {
                "kind": "metric_parse",
                "message": parse_failure,
            }

    if record["failure"] is not None:
        record["status"] = "failed"
    return record


def summarize(rounds):
    """Summarize timed rounds only; warmup is deliberately not an input."""
    return {
        "round_count": len(rounds),
        "total_native_queries": sum(
            item["native"]["query_count"] for item in rounds
        ),
        "median_qps": statistics.median(
            item["native"]["qps"] for item in rounds
        ),
        "min_qps": min(item["native"]["qps"] for item in rounds),
        "max_qps": max(item["native"]["qps"] for item in rounds),
        "median_wall_time_ms": statistics.median(
            item["wall_time_ms"] for item in rounds
        ),
        "median_native_latency_ms": {
            percentile: statistics.median(
                item["native"]["latency_ms"][percentile] for item in rounds
            )
            for percentile in ("p50", "p95", "p99")
        },
        "aggregation": "median across measured rounds; warmup excluded",
    }


def initial_result(args):
    """Create the run record before any external command starts."""
    query = workload_query(args.rows)
    warmup_iterations = args.warmup_iterations or min(args.iterations, 10)
    return {
        "schema_version": "kptools.clickhouse.benchmark.v1",
        "service": "clickhouse",
        "status": "running",
        "success": False,
        "scope": (
            "synthetic read/query-only SELECT; not TPC-H or production health"
        ),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": args.output,
        "target": {
            "host": args.host,
            "port": args.port,
            "user": args.resolved_user,
            "user_source": args.user_source,
            "secure": args.secure,
            "config_provided": args.config is not None,
            "single_binary": args.single_binary,
        },
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "preflight": {"status": "running", "checks": []},
        "workload": {
            "query": query,
            "read_only": True,
            "rows_per_query": args.rows,
            "expected_aggregate": str(args.rows * (args.rows - 1) // 2),
            "rounds": args.rounds,
            "iterations_per_round": args.iterations,
            "warmup_iterations": warmup_iterations,
            "concurrency": args.concurrency,
            "termination": "fixed iterations and finite subprocess timeouts",
            "correctness": (
                "exact aggregate checked separately after every phase"
            ),
        },
        "warmup": None,
        "rounds": [],
        "summary": None,
        "failure": None,
    }


def serializable_result(result):
    """Remove internal Path objects before emitting public JSON."""
    public = dict(result)
    public["output_dir"] = str(result["output_dir"])
    return public


def main(argv=None):
    """Run all phases, fail closed, and never overwrite previous evidence."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host or "\x00" in args.host:
        parser.error("host must be non-empty and contain no NUL")
    if args.user is not None and ("\x00" in args.user or not args.user):
        parser.error("--user must be non-empty and contain no NUL")
    if args.single_binary and args.benchmark != "clickhouse-benchmark":
        parser.error("--benchmark cannot be combined with --single-binary")
    try:
        resolve_user(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        print(f"cannot create fresh output directory: {exc}", file=sys.stderr)
        return 2

    result = initial_result(args)
    exit_code = 1
    try:
        free_bytes = shutil.disk_usage(args.output).free
        result["environment"]["output_free_bytes"] = free_bytes
        if free_bytes < 1024 * 1024:
            raise RunFailure("resource", "insufficient space for result logs")
        preflight(args, result)
        warmup = run_phase(
            args, result, "warmup", result["workload"]["warmup_iterations"]
        )
        result["warmup"] = warmup
        if warmup["status"] != "passed":
            raise RunFailure(
                warmup["failure"]["kind"],
                "warmup failed; timed rounds not run",
            )
        for index in range(1, args.rounds + 1):
            measured = run_phase(
                args, result, f"round-{index}", args.iterations
            )
            result["rounds"].append(measured)
            if measured["status"] != "passed":
                raise RunFailure(
                    measured["failure"]["kind"],
                    f"measured round {index} failed; later rounds not run",
                )
        result["summary"] = summarize(result["rounds"])
        result["status"] = "passed"
        result["success"] = True
        exit_code = 0
    except RunFailure as exc:
        result["status"] = "failed"
        if result["preflight"]["status"] == "running":
            result["preflight"]["status"] = "failed"
        result["failure"] = {"kind": exc.kind, "message": exc.message}
    except OSError as exc:
        result["status"] = "failed"
        if result["preflight"]["status"] == "running":
            result["preflight"]["status"] = "failed"
        result["failure"] = {"kind": "error", "message": redact(str(exc))}
        exit_code = 2
    except KeyboardInterrupt:
        result["status"] = "failed"
        result["failure"] = {"kind": "interrupted", "message": "interrupted"}
        exit_code = 130

    public = serializable_result(result)
    try:
        private_write(
            args.output / "result.json",
            json.dumps(public, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError as exc:
        print(f"cannot write result: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(public, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
