#!/usr/bin/env python3
"""Run a bounded, synthetic, SELECT-only Apache Doris benchmark.

No report from another skill is trusted. Every run performs new read-only SQL
prerequisite checks, starts real client processes, and validates exact results.
TCP argv handling was adapted from the user-supplied check_service.py
reference.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time



SCHEMA_VERSION = "kptools.doris.benchmark.v1"
SUPPORTED_DORIS = {(2, 1), (3, 0), (3, 1)}
SUPPORTED_TEXT = ("2.1.x", "3.0.x", "3.1.x")
DEFAULT_ROWS = 1_000_000
MAX_ROWS = 100_000_000
CAPABILITY_ROWS = 1000
CAPABILITY_SQL = 'SELECT SUM(number), COUNT(*) FROM numbers("number"="1000")'
CAPABILITY_RESULT = "499500\t1000"
EVIDENCE_LIMIT = 4096
DRAIN_TIMEOUT_SECONDS = 1.0
DEFAULTS_VALUE_OPTIONS = {
    "password",
    "default-auth",
    "server-public-key-path",
    "ssl-ca",
    "ssl-capath",
    "ssl-cert",
    "ssl-key",
    "ssl-cipher",
    "ssl-crl",
    "ssl-crlpath",
    "ssl-mode",
    "tls-version",
    "tls-ciphersuites",
}
DEFAULTS_FLAG_OPTIONS = {
    "connect-expired-password",
    "enable-cleartext-plugin",
    "get-server-public-key",
    "skip-ssl",
    "ssl",
    "ssl-verify-server-cert",
}


def expected_sum(rows):
    return rows * (rows - 1) // 2


def benchmark_sql(rows):
    return ('SELECT SUM(number), COUNT(*) FROM numbers("number"="{}")').format(
        rows
    )


def expected_result(rows):
    return "{}\t{}".format(expected_sum(rows), rows)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def finite_seconds(value):
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 300:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and between 0.1 and 300 seconds"
        )
    return seconds


def integer_range(minimum, maximum, label):
    def parse(value):
        try:
            number = int(value)
        except ValueError as exc:
            message = label + " must be an integer"
            raise argparse.ArgumentTypeError(message) from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                "{} must be between {} and {}".format(label, minimum, maximum)
            )
        return number

    return parse


def tcp_port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark an existing Doris service with a fixed synthetic "
            "SELECT. This is query-only and is not TPC-H."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=tcp_port, default=9030)
    parser.add_argument("--user", default="root")
    parser.add_argument("--client", default="mysql")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--timeout", type=finite_seconds, default=10.0)
    parser.add_argument(
        "--rows",
        type=integer_range(1, MAX_ROWS, "rows"),
        default=DEFAULT_ROWS,
        help=(
            "generated rows per query (default: 1000000; maximum: 100000000)"
        ),
    )
    parser.add_argument(
        "--rounds",
        type=integer_range(3, 20, "rounds"),
        default=3,
        help="timed rounds (minimum and default: 3; maximum: 20)",
    )
    parser.add_argument(
        "--queries-per-round",
        type=integer_range(1, 100, "queries-per-round"),
        default=5,
    )
    parser.add_argument(
        "--warmup",
        type=integer_range(1, 20, "warmup"),
        default=1,
        help="separate warmup queries, excluded from timed metrics",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; existing paths are never replaced",
    )
    return parser


def redact(text, secrets=()):
    value = text or ""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)(password|passwd|pwd)(\s*[=:]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        value,
    )


def clip(text):
    if len(text) <= EVIDENCE_LIMIT:
        return text
    return text[:EVIDENCE_LIMIT] + "\n[TRUNCATED]"


def kill_owned_group_and_drain(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=DRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "", ""


def true_value(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


def parse_table(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("result contains no data rows")
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        if len(values) != len(headers):
            raise ValueError("result row does not match its header")
        rows.append(dict(zip(headers, values)))
    return rows


def classify_client_error(stderr):
    lowered = stderr.lower()
    if any(
        marker in lowered
        for marker in ("error 1045", "access denied", "authentication failed")
    ):
        return "authentication"
    if any(
        marker in lowered
        for marker in (
            "error 1044",
            "command denied",
            "permission denied",
            "privilege",
        )
    ):
        return "authorization"
    if any(
        marker in lowered
        for marker in (
            "can't connect",
            "cannot connect",
            "connection refused",
            "unknown host",
            "name or service not known",
            "network is unreachable",
        )
    ):
        return "unreachable"
    return "runtime_error"


class PrerequisiteBlocked(ValueError):
    pass


class UnsupportedVersion(ValueError):
    pass


class DorisClient:
    def __init__(self, args):
        self.args = args
        self.secrets = [
            os.environ.get("DORIS_PASSWORD", ""),
            os.environ.get("MYSQL_PWD", ""),
        ]
        self.defaults_file = None
        self._config_directory = None
        self._home_directory = tempfile.TemporaryDirectory(
            prefix="doris-benchmark-home-"
        )

    def close(self):
        if self._config_directory is not None:
            self._config_directory.cleanup()
            self._config_directory = None
            self.defaults_file = None
        self._home_directory.cleanup()

    def prepare_credentials(self):
        env_password = "DORIS_PASSWORD" in os.environ or "MYSQL_PWD" in os.environ
        if self.args.config and env_password:
            raise PrerequisiteBlocked(
                "ambiguous_credentials: choose --config or a password environment"
            )
        if not self.args.config:
            return "environment" if env_password else "none"
        sanitized, passwords = sanitize_defaults_file(self.args.config)
        self.secrets.extend(passwords)
        self._config_directory = tempfile.TemporaryDirectory(
            prefix="doris-benchmark-config-"
        )
        path = Path(self._config_directory.name) / "client.cnf"
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(sanitized)
        self.defaults_file = path
        return "defaults_file"

    def child_env(self, include_password):
        home = Path(self._home_directory.name)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "MYSQL_TEST_LOGIN_FILE": str(home / ".mylogin.cnf"),
            "LANG": "C.UTF-8",
        }
        if include_password:
            if "DORIS_PASSWORD" in os.environ:
                env["MYSQL_PWD"] = os.environ["DORIS_PASSWORD"]
            elif "MYSQL_PWD" in os.environ:
                env["MYSQL_PWD"] = os.environ["MYSQL_PWD"]
        return env

    def execute(self, argv, include_password=False):
        timeout = self.args.timeout
        started_ns = time.monotonic_ns()
        outcome = {
            "returncode": None,
            "timed_out": False,
            "timeout_seconds": timeout,
            "stdout": "",
            "stderr": "",
        }
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=self.child_env(include_password),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            outcome.update(
                elapsed_ns=time.monotonic_ns() - started_ns,
                error="missing_tool",
                message=str(exc),
            )
            return outcome
        except OSError as exc:
            outcome.update(
                elapsed_ns=time.monotonic_ns() - started_ns,
                error="process_start_error",
                message=str(exc),
            )
            return outcome
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome["timed_out"] = True
            stdout, stderr = kill_owned_group_and_drain(process)
        except BaseException:
            kill_owned_group_and_drain(process)
            raise
        outcome.update(
            elapsed_ns=time.monotonic_ns() - started_ns,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        return outcome

    def evidence(self, outcome, sql=None):
        evidence = {
            key: outcome.get(key)
            for key in (
                "returncode",
                "elapsed_ns",
                "timed_out",
                "timeout_seconds",
            )
        }
        evidence["stdout"] = clip(redact(outcome["stdout"], self.secrets))
        evidence["stderr"] = clip(redact(outcome["stderr"], self.secrets))
        if sql is not None:
            evidence["sql"] = sql
        if outcome.get("error"):
            evidence["error"] = outcome["error"]
            evidence["message"] = clip(
                redact(outcome.get("message", ""), self.secrets)
            )
        return evidence

    def base_argv(self, sql, preserve_columns=False):
        argv = [self.args.client]
        if self.defaults_file is not None:
            argv.append("--defaults-file=" + str(self.defaults_file))
        else:
            argv.append("--no-defaults")
        argv.extend(
            [
                "--protocol=TCP",
                "--host",
                self.args.host,
                "--port",
                str(self.args.port),
                "--user",
                self.args.user,
                "--batch",
                "--raw",
            ]
        )
        if not preserve_columns:
            argv.append("--skip-column-names")
        argv.extend(
            [
                "--connect-timeout=" + str(math.ceil(self.args.timeout)),
                "--execute",
                sql,
            ]
        )
        return argv

    def query(self, sql, preserve_columns=False):
        return self.execute(
            self.base_argv(sql, preserve_columns), include_password=True
        )


def sanitize_defaults_file(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PrerequisiteBlocked(
            "insecure_config: not a regular non-symlink file"
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PrerequisiteBlocked("insecure_config: wrong file owner")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PrerequisiteBlocked(
            "insecure_config: group/other permissions set"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise PrerequisiteBlocked(
            "invalid_config: file is not valid UTF-8"
        ) from exc

    options = []
    passwords = []
    section = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("!"):
            raise PrerequisiteBlocked(
                "invalid_config: include directives are not allowed"
            )
        if line.startswith("["):
            if line not in {"[client]", "[mysql]"}:
                raise PrerequisiteBlocked(
                    "invalid_config: unsupported or malformed section"
                )
            section = line[1:-1]
            continue
        if section is None:
            raise PrerequisiteBlocked(
                "invalid_config: option outside client/mysql section"
            )
        if "=" in line:
            name, value = (part.strip() for part in line.split("=", 1))
            has_value = True
        else:
            name, value = line, None
            has_value = False
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise PrerequisiteBlocked(
                "invalid_config: unsupported or malformed option"
            )
        if name == "init-command":
            raise PrerequisiteBlocked(
                "invalid_config: init-command is not allowed"
            )
        if name in DEFAULTS_VALUE_OPTIONS:
            if not has_value or (not value and name != "password"):
                raise PrerequisiteBlocked(
                    "invalid_config: unsupported or malformed option"
                )
        elif name in DEFAULTS_FLAG_OPTIONS:
            if has_value:
                raise PrerequisiteBlocked(
                    "invalid_config: unsupported or malformed option"
                )
        else:
            raise PrerequisiteBlocked(
                "invalid_config: unsupported or malformed option"
            )
        if value is not None and "\x00" in value:
            raise PrerequisiteBlocked(
                "invalid_config: unsupported or malformed option"
            )
        options.append((name, value))
        if name == "password" and value:
            passwords.append(value)

    lines = ["[client]"]
    lines.extend(
        name if value is None else name + "=" + value
        for name, value in options
    )
    return "\n".join(lines) + "\n", passwords


def parse_select_one(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if lines != ["1"]:
        raise PrerequisiteBlocked("SELECT 1 returned an unexpected result")
    return {"exact_match": True, "expected": "1", "observed": "1"}


def parse_numbers_capability(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if lines != [CAPABILITY_RESULT]:
        raise PrerequisiteBlocked(
            "numbers(1000) did not return exactly 499500 and 1000"
        )
    return {
        "exact_match": True,
        "expected": CAPABILITY_RESULT,
        "observed": CAPABILITY_RESULT,
        "numbers_rows": CAPABILITY_ROWS,
        "numbers_sum": expected_sum(CAPABILITY_ROWS),
    }


def parse_frontends(text):
    rows = parse_table(text)
    required = {"Join", "Alive", "Version", "CurrentConnected"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise PrerequisiteBlocked("missing FE columns: " + ",".join(missing))
    connected = [row for row in rows if true_value(row["CurrentConnected"])]
    if not connected:
        raise PrerequisiteBlocked("no FE is marked CurrentConnected")
    current = connected[0]
    if not true_value(current["Join"]) or not true_value(current["Alive"]):
        raise PrerequisiteBlocked("connected FE is not both joined and alive")
    match = re.search(
        r"(?:doris[-_])?(\d+)\.(\d+)\.(\d+)",
        current["Version"],
        re.IGNORECASE,
    )
    if not match:
        raise PrerequisiteBlocked("connected FE Doris version is unrecognized")
    major, minor, patch = (int(part) for part in match.groups())
    if (major, minor) not in SUPPORTED_DORIS:
        raise UnsupportedVersion(
            "observed {}.{}.{}; supported interface lines are {}".format(
                major, minor, patch, ", ".join(SUPPORTED_TEXT)
            )
        )
    return {
        "doris_version": current["Version"],
        "parsed_version": "{}.{}.{}".format(major, minor, patch),
        "frontend_count": len(rows),
        "connected_frontend_ready": True,
    }


def parse_backends(text):
    rows = parse_table(text)
    if "Alive" not in rows[0]:
        raise PrerequisiteBlocked("missing BE column: Alive")
    usable = []
    for row in rows:
        if not true_value(row["Alive"]):
            continue
        if true_value(row.get("SystemDecommissioned", "false")):
            continue
        try:
            status = json.loads(row.get("Status", "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise PrerequisiteBlocked("BE Status is not valid JSON") from exc
        if not isinstance(status, dict):
            raise PrerequisiteBlocked("BE Status must be a JSON object")
        if status.get("isQueryDisabled") is True:
            continue
        usable.append(row)
    if not usable:
        raise PrerequisiteBlocked(
            "no alive, query-enabled, non-decommissioning BE"
        )
    return {
        "backend_count": len(rows),
        "query_capable_backend_count": len(usable),
    }


def run_preflight(client):
    probes = []
    aborted = False

    def add(name, status, reason, evidence=None):
        probes.append(
            {
                "name": name,
                "required": True,
                "status": status,
                "reason": reason,
                "evidence": evidence or {},
            }
        )

    try:
        method = client.prepare_credentials()
        add("credential_source", "ready", "ok", {"method": method})
    except (OSError, PrerequisiteBlocked) as exc:
        message = str(exc)
        reason = message.split(":", 1)[0]
        add("credential_source", "blocked", reason, {"detail": message})
        aborted = True

    if not aborted:
        outcome = client.execute([client.args.client, "--version"])
        evidence = client.evidence(outcome)
        version = outcome["stdout"].strip()
        if outcome.get("error") == "missing_tool":
            add("client", "blocked", "missing_tool", evidence)
            aborted = True
        elif outcome["timed_out"]:
            add("client", "blocked", "timeout", evidence)
            aborted = True
        elif outcome.get("error") or outcome["returncode"] != 0:
            add("client", "blocked", "runtime_error", evidence)
            aborted = True
        elif not version:
            add("client", "blocked", "version_unrecognized", evidence)
            aborted = True
        elif (
            "mysql" not in version.lower() and "mariadb" not in version.lower()
        ):
            add("client", "blocked", "unsupported_client", evidence)
            aborted = True
        else:
            evidence["reported_version"] = clip(redact(version, client.secrets))
            add("client", "ready", "ok", evidence)

    checks = (
        ("query_authentication", "SELECT 1", parse_select_one, False),
        (
            "frontend_compatibility",
            "SHOW FRONTENDS",
            parse_frontends,
            True,
        ),
        (
            "backend_prerequisites",
            "SHOW BACKENDS",
            parse_backends,
            True,
        ),
        (
            "numbers_capability",
            CAPABILITY_SQL,
            parse_numbers_capability,
            False,
        ),
    )
    for check_index, (name, sql, parser, preserve_columns) in enumerate(checks):
        if aborted:
            break
        outcome = client.query(sql, preserve_columns)
        evidence = client.evidence(outcome, sql)
        if outcome.get("error") == "missing_tool":
            add(name, "blocked", "missing_tool", evidence)
            aborted = True
            continue
        if outcome["timed_out"]:
            add(name, "blocked", "timeout", evidence)
            aborted = True
            continue
        if outcome.get("error"):
            add(name, "blocked", "runtime_error", evidence)
            aborted = True
            continue
        if outcome["returncode"] != 0:
            reason = classify_client_error(outcome["stderr"])
            add(name, "blocked", reason, evidence)
            aborted = True
            if name == "query_authentication" and reason == "authentication":
                for skipped_name, _, _, _ in checks[check_index + 1:]:
                    add(skipped_name, "skipped", "dependency_skipped")
            continue
        try:
            evidence.update(parser(outcome["stdout"]))
        except UnsupportedVersion as exc:
            add(
                name,
                "blocked",
                "unsupported_version",
                {**evidence, "detail": str(exc)},
            )
            aborted = True
            continue
        except (PrerequisiteBlocked, ValueError) as exc:
            add(
                name,
                "blocked",
                "invalid_prerequisite",
                {**evidence, "detail": str(exc)},
            )
            aborted = True
            continue
        add(name, "ready", "ok", evidence)
    return probes


def nearest_rank(values, percentile):
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def latency_metrics(records):
    elapsed_ns = [record["elapsed_ns"] for record in records]
    return {
        "count": len(elapsed_ns),
        "unit": "ms",
        "min": min(elapsed_ns) / 1_000_000,
        "mean": (sum(elapsed_ns) / len(elapsed_ns)) / 1_000_000,
        "p50": nearest_rank(elapsed_ns, 0.50) / 1_000_000,
        "p95": nearest_rank(elapsed_ns, 0.95) / 1_000_000,
        "max": max(elapsed_ns) / 1_000_000,
    }


def round_metrics(number, records, wall_elapsed_ns, rows):
    query_count = len(records)
    elapsed_seconds = wall_elapsed_ns / 1_000_000_000
    return {
        "round": number,
        "phase": "timed",
        "query_count": query_count,
        "successful_query_count": sum(
            record["status"] == "passed" for record in records
        ),
        "round_wall_elapsed_ns": wall_elapsed_ns,
        "query_latency": latency_metrics(records),
        "throughput": {
            "queries_per_second": query_count / elapsed_seconds,
            "unit": "queries/s",
            "basis": "query_count / round_wall_elapsed",
        },
        "validated_native_results": {
            "generated_rows_per_query": rows,
            "result_rows_per_query": 1,
            "sum_per_query": expected_sum(rows),
            "count_per_query": rows,
            "exact_matches": query_count,
        },
    }


def aggregate_metrics(rounds, timed_records):
    query_count = sum(item["query_count"] for item in rounds)
    wall_ns = sum(item["round_wall_elapsed_ns"] for item in rounds)
    elapsed_seconds = wall_ns / 1_000_000_000
    return {
        "timed_round_count": len(rounds),
        "completed_timed_round_count": len(rounds),
        "query_count": query_count,
        "round_wall_elapsed_ns": wall_ns,
        "query_latency": latency_metrics(timed_records),
        "throughput": {
            "queries_per_second": query_count / elapsed_seconds,
            "unit": "queries/s",
            "basis": "timed query_count / sum(round_wall_elapsed)",
        },
        "latency_scope": (
            "All per-query samples from complete timed rounds; "
            "warmup excluded."
        ),
        "warmup_included": False,
    }


def initial_result(args):
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "kptools-doris-benchmark",
        "status": "blocked",
        "success": False,
        "test_kind": "synthetic_numbers_select_only_benchmark",
        "is_tpch": False,
        "started_at": utc_now(),
        "target": {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "protocol": "TCP",
        },
        "compatibility": {
            "supported_doris_versions": list(SUPPORTED_TEXT),
            "basis": (
                "live probes of the exact SQL, SHOW fields, and numbers(1000) "
                "capability used in this run"
            ),
        },
        "workload": {
            "sql": benchmark_sql(args.rows),
            "sql_sha256": hashlib.sha256(
                benchmark_sql(args.rows).encode()
            ).hexdigest(),
            "expected_result_tsv": expected_result(args.rows),
            "generated_rows_per_query": args.rows,
            "expected_sum_per_query": expected_sum(args.rows),
            "expected_count_per_query": args.rows,
            "read_query_only": True,
            "rounds_requested": args.rounds,
            "queries_per_round": args.queries_per_round,
            "warmup_queries": args.warmup,
            "per_query_timeout_seconds": args.timeout,
            "concurrency": 1,
        },
        "measurement_scope": {
            "latency": (
                "client wall time for one new official CLI process, TCP "
                "connection, fixed query, result parsing, and process exit"
            ),
            "throughput": (
                "completed fixed queries divided by measured serial "
                "round wall time"
            ),
            "clock": "time.monotonic_ns",
            "server_side_profile": False,
        },
        "preflight": [],
        "warmup": {
            "queries_requested": args.warmup,
            "queries_completed": 0,
            "included_in_timed_metrics": False,
        },
        "rounds": [],
        "summary": None,
        "error": None,
        "limitations": [
            "Generated-numbers SELECT; not TPC-H or application QPS.",
            "No writes, imports, cache clearing, or production capacity "
            "claims.",
            "Each query includes a new client process and TCP connection.",
        ],
    }


def open_jsonl(path):
    return path.open("x", encoding="utf-8")


def write_record(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()


def execute_workload_query(client, rows, phase, iteration, round_number=None):
    started_at = utc_now()
    sql = benchmark_sql(rows)
    expected = expected_result(rows)
    outcome = client.query(sql)
    lines = [
        line for line in outcome["stdout"].strip().splitlines() if line.strip()
    ]
    observed = clip(redact("\n".join(lines), client.secrets))
    exact = outcome["returncode"] == 0 and lines == [expected]
    if outcome["timed_out"]:
        reason = "timeout"
    elif outcome.get("error") == "missing_tool":
        reason = "missing_tool"
    elif outcome.get("error"):
        reason = "runtime_error"
    elif outcome["returncode"] != 0:
        reason = classify_client_error(outcome["stderr"])
    elif not exact:
        reason = "correctness_mismatch"
    else:
        reason = "ok"
    record = {
        "phase": phase,
        "round": round_number,
        "iteration": iteration,
        "started_at": started_at,
        "status": "passed" if exact else "failed",
        "reason": reason,
        "generated_rows": rows,
        "correctness": {
            "expected_tsv": expected,
            "observed_tsv": observed,
            "expected_sum": expected_sum(rows),
            "expected_count": rows,
            "exact_match": exact,
        },
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
    }
    record.update(client.evidence(outcome, sql))
    return record


def set_failure(result, record):
    result["status"] = "failed"
    result["success"] = False
    result["error"] = {
        "phase": record["phase"],
        "round": record["round"],
        "iteration": record["iteration"],
        "reason": record["reason"],
        "detail": "raw redacted evidence is preserved in queries.jsonl",
    }


def run_benchmark(args, output):
    client = DorisClient(args)
    try:
        return run_benchmark_with_client(args, output, client)
    finally:
        client.close()


def run_benchmark_with_client(args, output, client):
    result = initial_result(args)
    result["preflight"] = run_preflight(client)
    if any(probe["status"] != "ready" for probe in result["preflight"]):
        result["error"] = {
            "phase": "preflight",
            "reason": "live_prerequisites_not_met",
        }
        return result

    with open_jsonl(output / "queries.jsonl") as raw_stream:
        for iteration in range(1, args.warmup + 1):
            record = execute_workload_query(
                client, args.rows, "warmup", iteration
            )
            write_record(raw_stream, record)
            if record["status"] != "passed":
                set_failure(result, record)
                return result
            result["warmup"]["queries_completed"] += 1

        timed_records = []
        for round_number in range(1, args.rounds + 1):
            records = []
            round_started_ns = time.monotonic_ns()
            for iteration in range(1, args.queries_per_round + 1):
                record = execute_workload_query(
                    client, args.rows, "timed", iteration, round_number
                )
                records.append(record)
                write_record(raw_stream, record)
                if record["status"] != "passed":
                    set_failure(result, record)
                    return result
            wall_ns = time.monotonic_ns() - round_started_ns
            if wall_ns <= 0:
                raise RuntimeError("monotonic clock did not advance")
            timed_records.extend(records)
            result["rounds"].append(
                round_metrics(round_number, records, wall_ns, args.rows)
            )

    result["summary"] = aggregate_metrics(result["rounds"], timed_records)
    result["status"] = "passed"
    result["success"] = True
    return result


def write_result(output, result):
    result["finished_at"] = utc_now()
    path = output / "result.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def prepare_output(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=False)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        prepare_output(args.output)
    except OSError as exc:
        print(
            json.dumps({"status": "error", "detail": str(exc)}),
            file=sys.stderr,
        )
        return 2
    try:
        result = run_benchmark(args, args.output)
        write_result(args.output, result)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "detail": redact(str(exc)),
                    "preserved_output": str(args.output),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(args.output / "result.json"),
                "rounds": len(result["rounds"]),
                "error": result["error"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
