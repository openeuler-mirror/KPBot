#!/usr/bin/env python3
"""Read-only Apache Doris benchmark prerequisite probes.

The TCP-only client construction and exact-result checks were adapted from the
user-supplied check_service.py reference. This implementation is
self-contained.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


SCHEMA_VERSION = "kptools.doris.environment.v1"
SUPPORTED_DORIS = {(2, 1), (3, 0), (3, 1)}
SUPPORTED_TEXT = ("2.1.x", "3.0.x", "3.1.x")
CAPABILITY_SQL = 'SELECT SUM(number), COUNT(*) FROM numbers("number"="1000")'
CAPABILITY_RESULT = "499500\t1000"
OUTPUT_BYTES_REQUIRED = 1024 * 1024
EVIDENCE_LIMIT = 4096
DRAIN_TIMEOUT_SECONDS = 1.0

EXIT_READY = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2
EXIT_UNKNOWN = 3


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
            "Probe an existing Doris 2.1/3.0/3.1 service without modifying it."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=tcp_port, default=9030)
    parser.add_argument("--user", default="root")
    parser.add_argument("--client", default="mysql")
    parser.add_argument(
        "--config",
        type=Path,
        help="owner-only MySQL defaults file; never printed",
    )
    parser.add_argument("--timeout", type=finite_seconds, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def redact(text, secrets=()):
    value = text or ""
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"(?i)(password|passwd|pwd)(\s*[=:]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        value,
    )
    return value


def clip(text):
    if len(text) <= EVIDENCE_LIMIT:
        return text
    return text[:EVIDENCE_LIMIT] + "\n[TRUNCATED]"


ALLOWED_CONFIG_OPTIONS = {
    "default-auth",
    "enable-cleartext-plugin",
    "get-server-public-key",
    "password",
    "plugin-dir",
    "server-public-key-path",
    "skip-ssl",
    "ssl",
    "ssl-ca",
    "ssl-capath",
    "ssl-cert",
    "ssl-cipher",
    "ssl-fips-mode",
    "ssl-key",
    "ssl-mode",
    "ssl-verify-server-cert",
    "tls-ciphersuites",
    "tls-version",
}
BOOLEAN_CONFIG_OPTIONS = {
    "enable-cleartext-plugin",
    "get-server-public-key",
    "skip-ssl",
    "ssl",
    "ssl-verify-server-cert",
}


def validated_defaults(path, destination):
    """Copy only explicit authentication/TLS options to an ephemeral file."""
    lines = ["[client]"]
    passwords = []
    section = None
    seen = set()
    with path.open("r", encoding="utf-8") as stream:
        for number, original in enumerate(stream, 1):
            stripped = original.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            if stripped.startswith("!"):
                raise ValueError("external configuration includes are forbidden")
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip().lower()
                if section not in {"client", "mysql"}:
                    raise ValueError("unsupported configuration section")
                continue
            if section not in {"client", "mysql"}:
                raise ValueError("option outside [client] or [mysql]")
            if "=" in stripped:
                key, value = stripped.split("=", 1)
                key = key.strip().lower().replace("_", "-")
                value = value.strip()
            else:
                fields = stripped.split(None, 1)
                key = fields[0].lower().replace("_", "-")
                value = fields[1].strip() if len(fields) == 2 else ""
            if key in {"init-command", "init-connect"}:
                raise ValueError("initialization SQL is forbidden")
            if key not in ALLOWED_CONFIG_OPTIONS:
                raise ValueError("unsupported configuration option")
            if key in seen or (not value and key not in BOOLEAN_CONFIG_OPTIONS):
                raise ValueError("duplicate or malformed configuration option")
            if "\x00" in value:
                raise ValueError("malformed configuration value")
            seen.add(key)
            if key == "password" and value:
                passwords.append(value.strip("'\""))
            lines.append(key + ("=" + value if value else ""))
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return passwords


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def kill_owned_group_and_drain(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=DRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "", ""


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


def true_value(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


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


class ProbeRunner:
    def __init__(self, args):
        self.args = args
        self.probes = []
        self.secrets = [
            os.environ.get("DORIS_PASSWORD", ""),
            os.environ.get("MYSQL_PWD", ""),
        ]
        self.abort = False
        self.authentication_failed = False
        self.validated_config = None
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="kptools-doris-environment-"
        )

    def close(self):
        self.temp_dir.cleanup()

    def add(self, name, required, status, reason, evidence=None):
        probe = {
            "name": name,
            "required": required,
            "status": status,
            "reason": reason,
            "evidence": evidence or {},
        }
        self.probes.append(probe)
        return probe

    def child_env(self, include_password):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": self.temp_dir.name,
            "MYSQL_TEST_LOGIN_FILE": str(
                Path(self.temp_dir.name) / "no-saved-login.cnf"
            ),
            "LANG": "C.UTF-8",
        }
        if include_password:
            if "DORIS_PASSWORD" in os.environ:
                env["MYSQL_PWD"] = os.environ["DORIS_PASSWORD"]
            elif "MYSQL_PWD" in os.environ:
                env["MYSQL_PWD"] = os.environ["MYSQL_PWD"]
        return env

    def execute(self, argv, timeout, include_password=False):
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
        outcome["elapsed_ns"] = time.monotonic_ns() - started_ns
        outcome["returncode"] = process.returncode
        outcome["raw_stdout"] = stdout
        outcome["raw_stderr"] = stderr
        outcome["stdout"] = clip(redact(stdout, self.secrets))
        outcome["stderr"] = clip(redact(stderr, self.secrets))
        return outcome

    def command_evidence(self, outcome, sql=None):
        evidence = {
            "returncode": outcome["returncode"],
            "elapsed_ns": outcome["elapsed_ns"],
            "timed_out": outcome["timed_out"],
            "timeout_seconds": outcome["timeout_seconds"],
            "stdout": outcome.get("stdout", ""),
            "stderr": outcome.get("stderr", ""),
            "stdout_sha256": sha256_text(outcome.get("stdout", "")),
        }
        if sql is not None:
            evidence["sql"] = sql
        if outcome.get("error"):
            evidence["error"] = outcome["error"]
            evidence["message"] = clip(
                redact(outcome.get("message", ""), self.secrets)
            )
        return evidence

    def credential_probe(self):
        config = self.args.config
        env_password = (
            "DORIS_PASSWORD" in os.environ or "MYSQL_PWD" in os.environ
        )
        if config and env_password:
            self.abort = True
            return self.add(
                "credential_source",
                True,
                "blocked",
                "ambiguous_credentials",
                {"detail": "choose either --config or a password environment"},
            )
        if not config:
            return self.add(
                "credential_source",
                True,
                "ready",
                "ok",
                {"method": "environment" if env_password else "none"},
            )
        try:
            info = config.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("config must be a regular non-symlink file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ValueError("config must be owned by the current user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(
                    "config permissions must not allow group/other"
                )
        except (OSError, ValueError) as exc:
            self.abort = True
            return self.add(
                "credential_source",
                True,
                "blocked",
                "insecure_config",
                {"detail": str(exc)},
            )
        try:
            self.validated_config = Path(self.temp_dir.name) / "client.cnf"
            self.secrets.extend(
                validated_defaults(config, self.validated_config)
            )
        except (OSError, UnicodeError, ValueError) as exc:
            self.validated_config = None
            self.abort = True
            return self.add(
                "credential_source",
                True,
                "blocked",
                "insecure_config",
                {"detail": str(exc)},
            )
        return self.add(
            "credential_source",
            True,
            "ready",
            "ok",
            {
                "method": "defaults_file",
                "contents": "validated_authentication_and_tls_only",
            },
        )

    def storage_probe(self):
        try:
            usage = shutil.disk_usage(self.args.output.parent)
        except OSError as exc:
            self.abort = True
            return self.add(
                "output_storage",
                True,
                "blocked",
                "storage_error",
                {"detail": str(exc)},
            )
        status = "ready" if usage.free >= OUTPUT_BYTES_REQUIRED else "blocked"
        reason = "ok" if status == "ready" else "insufficient_output_space"
        if status == "blocked":
            self.abort = True
        return self.add(
            "output_storage",
            True,
            status,
            reason,
            {
                "free_bytes": usage.free,
                "required_free_bytes": OUTPUT_BYTES_REQUIRED,
                "scope": "small JSON report only; not Doris data capacity",
            },
        )

    def client_probe(self):
        if self.abort:
            return None
        outcome = self.execute(
            [self.args.client, "--version"], self.args.timeout
        )
        evidence = self.command_evidence(outcome)
        if outcome.get("error") == "missing_tool":
            self.abort = True
            return self.add(
                "client", True, "blocked", "missing_tool", evidence
            )
        if outcome["timed_out"]:
            self.abort = True
            return self.add("client", True, "blocked", "timeout", evidence)
        if outcome.get("error") or outcome["returncode"] != 0:
            self.abort = True
            return self.add(
                "client", True, "blocked", "runtime_error", evidence
            )
        version = outcome["stdout"].strip()
        lowered = version.lower()
        if not version:
            self.abort = True
            return self.add(
                "client", True, "unknown", "version_unrecognized", evidence
            )
        if "mysql" not in lowered and "mariadb" not in lowered:
            self.abort = True
            return self.add(
                "client", True, "blocked", "unsupported_client", evidence
            )
        evidence["reported_version"] = version
        evidence["compatibility"] = (
            "validated by subsequent live SQL probes, not version text alone"
        )
        return self.add("client", True, "ready", "ok", evidence)

    def base_argv(self, sql, preserve_columns=False):
        argv = [self.args.client]
        if self.validated_config:
            argv.append("--defaults-file=" + str(self.validated_config))
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

    def sql_probe(self, name, sql, parser, preserve_columns=False):
        if self.abort:
            if self.authentication_failed:
                return self.add(
                    name,
                    True,
                    "skipped",
                    "dependency_skipped",
                    {"dependency": "query_authentication"},
                )
            return None
        outcome = self.execute(
            self.base_argv(sql, preserve_columns),
            self.args.timeout,
            include_password=True,
        )
        evidence = self.command_evidence(outcome, sql)
        if outcome.get("error") == "missing_tool":
            self.abort = True
            return self.add(name, True, "blocked", "missing_tool", evidence)
        if outcome["timed_out"]:
            return self.add(name, True, "blocked", "timeout", evidence)
        if outcome.get("error"):
            return self.add(name, True, "blocked", "runtime_error", evidence)
        if outcome["returncode"] != 0:
            reason = classify_client_error(outcome.get("raw_stderr", ""))
            if name == "query_authentication" and reason == "authentication":
                self.authentication_failed = True
                self.abort = True
            return self.add(name, True, "blocked", reason, evidence)
        try:
            extra = parser(outcome["raw_stdout"])
        except UnsupportedVersion as exc:
            return self.add(
                name,
                True,
                "blocked",
                "unsupported_version",
                {**evidence, "detail": str(exc)},
            )
        except PrerequisiteBlocked as exc:
            return self.add(
                name,
                True,
                "blocked",
                "invalid_prerequisite",
                {**evidence, "detail": str(exc)},
            )
        except InconclusiveResult as exc:
            return self.add(
                name,
                True,
                "unknown",
                "inconclusive_result",
                {**evidence, "detail": str(exc)},
            )
        except ValueError as exc:
            return self.add(
                name,
                True,
                "unknown",
                "invalid_result",
                {**evidence, "detail": str(exc)},
            )
        evidence.update(extra)
        return self.add(name, True, "ready", "ok", evidence)


class UnsupportedVersion(ValueError):
    pass


class PrerequisiteBlocked(ValueError):
    pass


class InconclusiveResult(ValueError):
    pass


def parse_select_one(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if lines != ["1"]:
        raise InconclusiveResult("SELECT 1 did not return exactly one row: 1")
    return {"expected": "1", "observed": "1", "exact_match": True}


def parse_numbers_capability(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if lines != [CAPABILITY_RESULT]:
        raise PrerequisiteBlocked(
            "numbers(1000) did not return exactly 499500 and 1000"
        )
    return {
        "expected": CAPABILITY_RESULT,
        "observed": CAPABILITY_RESULT,
        "exact_match": True,
        "numbers_rows": 1000,
        "numbers_sum": 499500,
    }


def parse_frontends(text):
    rows = parse_table(text)
    required = {"Join", "Alive", "Version", "CurrentConnected"}
    missing = sorted(required - set(rows[0]))
    if missing:
        raise InconclusiveResult("missing FE columns: " + ",".join(missing))
    connected = [row for row in rows if true_value(row["CurrentConnected"])]
    if not connected:
        raise InconclusiveResult("no FE is marked CurrentConnected")
    current = connected[0]
    if not true_value(current["Join"]) or not true_value(current["Alive"]):
        raise PrerequisiteBlocked("connected FE is not both joined and alive")
    match = re.search(
        r"(?:doris[-_])?(\d+)\.(\d+)\.(\d+)", current["Version"], re.IGNORECASE
    )
    if not match:
        raise InconclusiveResult("connected FE Doris version is unrecognized")
    major, minor, patch = (int(part) for part in match.groups())
    if (major, minor) not in SUPPORTED_DORIS:
        raise UnsupportedVersion(
            "observed {}.{}.{}; supported interface lines are {}".format(
                major, minor, patch, ", ".join(SUPPORTED_TEXT)
            )
        )
    return {
        "frontend_count": len(rows),
        "connected_frontend": {
            "join": current["Join"],
            "alive": current["Alive"],
            "current_connected": current["CurrentConnected"],
        },
        "doris_version": current["Version"],
        "parsed_version": "{}.{}.{}".format(major, minor, patch),
        "supported_interface": True,
    }


def parse_backends(text):
    rows = parse_table(text)
    if "Alive" not in rows[0]:
        raise InconclusiveResult("missing BE column: Alive")
    usable = []
    for row in rows:
        if not true_value(row["Alive"]):
            continue
        if true_value(row.get("SystemDecommissioned", "false")):
            continue
        try:
            status = json.loads(row.get("Status", "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise InconclusiveResult("BE Status is not valid JSON") from exc
        if not isinstance(status, dict):
            raise InconclusiveResult("BE Status JSON must be an object")
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
        "scope": "cluster membership evidence; not a production health check",
    }


def initial_result(args):
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": "kptools-doris-environment-check",
        "status": "unknown",
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
                "SELECT 1, SHOW FRONTENDS/BACKENDS fields, and the "
                "numbers(1000) table function used by the bundled query-only "
                "benchmark; other versions fail closed"
            ),
        },
        "probes": [],
        "limitations": [
            "Read-only connectivity and synthetic-query prerequisites only.",
            "No writes, imports, production health, or sustained load are "
            "tested.",
            "Client and server exact versions are evidence, not broad "
            "support claims.",
        ],
    }


def finalize_result(result, started_ns):
    probes = result["probes"]
    blocked = any(
        item["required"] and item["status"] == "blocked" for item in probes
    )
    unknown = any(item["status"] == "unknown" for item in probes)
    if blocked:
        result["status"] = "blocked"
    elif unknown:
        result["status"] = "unknown"
    else:
        result["status"] = "ready"
    result["summary"] = {
        status: sum(item["status"] == status for item in probes)
        for status in ("ready", "blocked", "unknown", "skipped")
    }
    result["finished_at"] = utc_now()
    result["elapsed_ns"] = time.monotonic_ns() - started_ns
    return result


def write_exclusive(path, result):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def prepare_output(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError("output already exists: " + str(path))


def run(args):
    started_ns = time.monotonic_ns()
    result = initial_result(args)
    runner = ProbeRunner(args)
    try:
        runner.credential_probe()
        runner.storage_probe()
        runner.client_probe()
        runner.sql_probe("query_authentication", "SELECT 1", parse_select_one)
        runner.sql_probe(
            "frontend_compatibility",
            "SHOW FRONTENDS",
            parse_frontends,
            preserve_columns=True,
        )
        runner.sql_probe(
            "backend_prerequisites",
            "SHOW BACKENDS",
            parse_backends,
            preserve_columns=True,
        )
        runner.sql_probe(
            "numbers_capability", CAPABILITY_SQL, parse_numbers_capability
        )
        result["probes"] = runner.probes
        return finalize_result(result, started_ns)
    finally:
        runner.close()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        prepare_output(args.output)
    except OSError as exc:
        print(
            json.dumps({"status": "error", "detail": str(exc)}),
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        result = run(args)
        write_exclusive(args.output, result)
    except (OSError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "detail": redact(str(exc))}),
            file=sys.stderr,
        )
        return EXIT_USAGE
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(args.output),
                "summary": result["summary"],
            },
            ensure_ascii=False,
        )
    )
    if result["status"] == "ready":
        return EXIT_READY
    if result["status"] == "blocked":
        return EXIT_BLOCKED
    return EXIT_UNKNOWN


if __name__ == "__main__":
    sys.exit(main())
