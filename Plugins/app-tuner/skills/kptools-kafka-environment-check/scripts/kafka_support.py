"""Stdlib-only Kafka CLI support for this independently installed skill.

Includes self-contained Java-properties parsing and Kafka CLI probes.
"""

from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re
import signal
import stat
import string
import subprocess
import time


SUPPORTED_VERSIONS = ((4, 4),)
MINIMUM_JAVA_MAJOR = 17
ALLOWED_CONFIG_KEYS = {
    "client.dns.lookup",
    "connections.max.idle.ms",
    "default.api.timeout.ms",
    "metadata.max.age.ms",
    "reconnect.backoff.max.ms",
    "reconnect.backoff.ms",
    "request.timeout.ms",
    "security.protocol",
    "socket.connection.setup.timeout.max.ms",
    "socket.connection.setup.timeout.ms",
}
ALLOWED_CONFIG_PREFIXES = ("sasl.", "ssl.")
AUTH_MARKERS = (
    "authenticationexception",
    "authorizationexception",
    "saslauthenticationexception",
    "sslhandshakeexception",
    "access denied",
    "permission denied",
    "authentication failed",
    "authorization failed",
)
TIMEOUT_MARKERS = (
    "timeoutexception",
    "timed out",
    "timeout expired",
    "request timed out",
)
DRAIN_TIMEOUT_SECONDS = 5.0
SENSITIVE_CONFIG_MARKERS = (
    "credential",
    "password",
    "private.key",
    "secret",
    "token",
)
CLI_ENVIRONMENT_KEYS = (
    "HOME",
    "JAVA_HOME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    # Native Kerberos configuration/cache locations used by Java GSSAPI.
    "KRB5_CLIENT_KTNAME",
    "KRB5_CONFIG",
    "KRB5_KTNAME",
    "KRB5_TRACE",
    "KRB5CCNAME",
)


class ConfigError(ValueError):
    """A secure client configuration cannot be used safely."""

    def __init__(self, message, category="invalid_config"):
        super().__init__(message)
        self.category = category


class ClientConfig:
    """A validated config path with values available only for redaction."""

    def __init__(self, path=None, properties=None, digest=None, mode=None):
        self.path = path
        self.properties = properties or {}
        self.digest = digest
        self.mode = mode

    @property
    def redactions(self):
        values = set()
        for key, value in self.properties.items():
            if not value:
                continue
            if _sensitive_config_key(key):
                values.add(value)
            if key.lower() == "sasl.jaas.config":
                values.update(_jaas_credentials(value))
        return sorted(values, key=len, reverse=True)

    def evidence(self):
        return {
            "provided": self.path is not None,
            "sha256": self.digest,
            "file_mode": self.mode,
            "property_keys": sorted(self.properties),
            "property_values": "redacted",
        }


def _jaas_credentials(text):
    pattern = re.compile(
        r"(?i)(?:credential|password|private_key|secret|token|username)"
        r"\s*=\s*(?:\"((?:\\.|[^\"\\])*)\"|'([^']*)'|([^\s;]+))"
    )
    return {
        next(group for group in match.groups() if group is not None)
        for match in pattern.finditer(text)
        if any(group for group in match.groups() if group is not None)
    }


def _sensitive_config_key(key):
    normalized = key.lower()
    return normalized == "sasl.jaas.config" or any(
        marker in normalized
        for marker in (*SENSITIVE_CONFIG_MARKERS, "key.pem", "keystore.key")
    )


def _java_unescape(text):
    result = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        index += 1
        if index >= len(text):
            result.append("\\")
            break
        char = text[index]
        if char == "u":
            digits = text[index + 1 : index + 5]
            if len(digits) != 4 or any(
                digit not in string.hexdigits for digit in digits
            ):
                raise ConfigError(
                    "client config has an invalid Unicode escape"
                )
            result.append(chr(int(digits, 16)))
            index += 5
            continue
        escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
        result.append(escapes.get(char, char))
        index += 1
    return "".join(result)


def _java_physical_lines(text):
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            yield text[start:index]
            index += 1
            if index < len(text) and text[index] == "\n":
                index += 1
            start = index
        elif char == "\n":
            yield text[start:index]
            index += 1
            start = index
        else:
            index += 1
    if start < len(text):
        yield text[start:]


def _java_logical_lines(text):
    if text.startswith("\ufeff"):
        text = text[1:]
    buffer = ""
    continued = False
    for line in _java_physical_lines(text):
        line = line.lstrip(" \t\f")
        if not continued:
            if not line or line[0] in "#!":
                continue
            buffer = line
        else:
            buffer += line
        if (len(buffer) - len(buffer.rstrip("\\"))) % 2:
            buffer = buffer[:-1]
            continued = True
            continue
        yield buffer
        buffer = ""
        continued = False
    if buffer or continued:
        yield buffer


def java_properties(text):
    """Parse Java properties without evaluating arbitrary expressions."""
    properties = {}
    for line in _java_logical_lines(text):
        line = line.lstrip(" \t\f")
        if not line or line[0] in "#!":
            continue
        escaped = False
        separator = None
        key_end = len(line)
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char in "=:" or char in " \t\f":
                key_end = index
                separator = char
                break
        key = _java_unescape(line[:key_end])
        if not key:
            continue
        if separator is None:
            value = ""
        else:
            value = line[key_end + 1 :]
            if separator in " \t\f":
                value = value.lstrip(" \t\f")
                if value[:1] in ("=", ":"):
                    value = value[1:]
                value = value.lstrip(" \t\f")
        properties[key] = _java_unescape(value)
    return properties


def load_client_config(path):
    """Validate a transport/auth-only file without disclosing its values."""
    if path is None:
        return ClientConfig()
    supplied = Path(path).expanduser()
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(
            "client config cannot be resolved: " + exc.strerror,
            "absent",
        ) from exc
    if not resolved.is_file():
        raise ConfigError("client config is not a regular file", "absent")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ConfigError(
            "client config metadata cannot be read: " + exc.strerror,
            "error",
        ) from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name == "posix":
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ConfigError(
                "client config must be owned by the current user",
                "insecure_config",
            )
        if mode & 0o077:
            raise ConfigError(
                "client config permissions must not grant group/other access",
                "insecure_config",
            )
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigError(
            "client config cannot be read: " + exc.strerror,
            "error",
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    properties = java_properties(text)
    for key in properties:
        if key not in ALLOWED_CONFIG_KEYS and not key.startswith(
            ALLOWED_CONFIG_PREFIXES
        ):
            raise ConfigError(
                "client config key is outside the transport/auth allowlist: "
                + key
            )
    return ClientConfig(
        path=resolved,
        properties=properties,
        digest=hashlib.sha256(raw).hexdigest(),
        mode=format(mode, "04o"),
    )


def redact(text, secrets=()):
    """Remove supplied config values and common inline secret assignments."""
    if text is None:
        return ""
    safe = str(text)
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "<redacted>")
    pattern = re.compile(
        r"(?i)((?:password|secret|token)\s*[=:]\s*)([^\s,;]+)"
    )
    return pattern.sub(r"\1<redacted>", safe)


CONSUMER_STDOUT_BANNERS = frozenset(
    (
        "Option --consumer.config is deprecated and will be removed in a "
        "future version. Use --command-config instead.",
        "Option --consumer-property is deprecated and will be removed in "
        "a future version. Use --command-property instead.",
    )
)


def consumed_payload_lines(text):
    """Return only consumed payload lines, not exact CLI deprecation banners.

    Kafka 4.4 console consumer prints deprecation notices for supported
    options on stdout. Dropping only those exact banner lines keeps the
    downstream exact-payload comparison strict; any other line still
    counts as consumed output.
    """
    return [
        line
        for line in (text or "").splitlines()
        if line not in CONSUMER_STDOUT_BANNERS
    ]


def topic_query_pattern(topic):
    """Escape a literal topic for Kafka CLI options that accept regexes."""
    return re.escape(topic)


def parse_topic_description(text, expected_topic):
    """Select only the exact topic header and derive its partition set."""
    matches = []
    for line in (text or "").splitlines():
        match = re.match(
            r"^\s*Topic:\s*(\S+)(?:\s+TopicId:\s*\S+)?\s+"
            r"PartitionCount:\s*(\d+)\s+ReplicationFactor:\s*(\d+)(?:\s|$)",
            line,
        )
        if match and match.group(1) == expected_topic:
            matches.append(match)
    if len(matches) != 1:
        raise ValueError(
            "describe output must contain exactly one exact target topic "
            "header"
        )
    match = matches[0]
    partition_count = int(match.group(2))
    if partition_count < 1:
        raise ValueError("describe output contains no target partitions")
    return {
        "topic": expected_topic,
        "partition_count": partition_count,
        "replication_factor": int(match.group(3)),
        "partitions": list(range(partition_count)),
    }


def parse_latest_offsets(text, expected_topic, expected_partitions):
    """Return exact-topic offsets only when every described partition appears."""
    expected = set(expected_partitions)
    offsets = {}
    pattern = re.compile(
        re.escape(expected_topic) + r":(\d+):(\d+)"
    )
    for line in (text or "").splitlines():
        match = pattern.fullmatch(line.strip())
        if not match:
            continue
        partition = int(match.group(1))
        if partition in offsets:
            raise ValueError("offset output repeats a target partition")
        offsets[partition] = int(match.group(2))
    missing = sorted(expected - offsets.keys())
    unexpected = sorted(offsets.keys() - expected)
    if not expected or missing or unexpected:
        raise ValueError(
            "offset output does not exactly cover the described partitions; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return offsets


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def terminate_and_drain(process):
    """Kill only the owned child group and drain with a second hard bound."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    try:
        stdout, stderr = process.communicate(timeout=DRAIN_TIMEOUT_SECONDS)
        return stdout, stderr, False
    except subprocess.TimeoutExpired as exc:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=DRAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        stdout = exc.output or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return stdout, stderr, True


def build_cli_environment():
    """Return only the runtime variables required by official Java CLIs."""
    return {
        key: os.environ[key]
        for key in CLI_ENVIRONMENT_KEYS
        if key in os.environ
    }


def run_process(argv, timeout, input_text=None, redactions=()):
    """Run one bounded process group and return sanitized native evidence."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("process timeout must be finite and positive")
    started = time.monotonic_ns()
    result = {
        "command": [redact(item, redactions) for item in argv],
        "returncode": None,
        "timed_out": False,
        "drain_timed_out": False,
        "started_at_utc": utc_now(),
        "duration_ms": None,
        "stdout": "",
        "stderr": "",
        "error_category": None,
    }

    def finish():
        result["duration_ms"] = round(
            (time.monotonic_ns() - started) / 1_000_000, 3
        )
        result["stdout"] = redact(result["stdout"], redactions)
        result["stderr"] = redact(result["stderr"], redactions)
        return result

    try:
        process = subprocess.Popen(
            argv,
            env=build_cli_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        result["error_category"] = (
            "absent" if isinstance(exc, FileNotFoundError) else "error"
        )
        result["stderr"] = str(exc)
        return finish()
    try:
        stdout, stderr = process.communicate(
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        stdout, stderr, drain_timed_out = terminate_and_drain(process)
        result["drain_timed_out"] = drain_timed_out
    except BaseException as exc:
        result["error_category"] = "interrupted"
        stdout, stderr, drain_timed_out = terminate_and_drain(process)
        result["drain_timed_out"] = drain_timed_out
        result["returncode"] = process.returncode
        result["stdout"] = stdout
        result["stderr"] = stderr
        exc.process_result = finish()
        raise
    result["returncode"] = process.returncode
    result["stdout"] = stdout
    result["stderr"] = stderr
    return finish()


def run_cli(
    kafka_dir,
    tool,
    parameters,
    timeout,
    client_config=None,
    config_flag=None,
    input_text=None,
):
    """Invoke an installed official CLI without shell interpolation."""
    config = client_config or ClientConfig()
    argv = ["bash", str(Path(kafka_dir) / "bin" / tool)]
    argv.extend(str(item) for item in parameters)
    if config.path is not None and config_flag:
        argv.extend((config_flag, str(config.path)))
    return run_process(
        argv,
        timeout,
        input_text=input_text,
        redactions=config.redactions,
    )


def error_category(result):
    """Classify absent tools, auth, timeout and other runtime failures."""
    if result.get("error_category"):
        return result["error_category"]
    if result.get("timed_out"):
        return "timeout"
    # Successful stdout is command data (for example, a topic list), not a
    # diagnostic stream. A topic named "audit-timeoutexception" must not make
    # an otherwise successful query look like a timeout. On failures, stdout
    # can carry explicit Java/Kafka error records and remains diagnostic.
    output = result.get("stderr", "")
    if result.get("returncode") not in (0, None):
        output = result.get("stdout", "") + "\n" + output
    output = output.lower()
    if any(marker in output for marker in AUTH_MARKERS):
        return "auth"
    if any(marker in output for marker in TIMEOUT_MARKERS):
        return "timeout"
    if result.get("returncode") not in (0, None):
        return "error"
    if result.get("returncode") is None:
        return "error"
    return None


def public_command(result):
    """Return process evidence without copying native output into a probe."""
    return {
        "command": result["command"],
        "returncode": result["returncode"],
        "timed_out": result["timed_out"],
        "drain_timed_out": result["drain_timed_out"],
        "started_at_utc": result["started_at_utc"],
        "duration_ms": result["duration_ms"],
    }


def write_command_logs(output_dir, stem, result):
    """Preserve sanitized stdout and stderr for a single command."""
    stdout_path = Path(output_dir) / (stem + ".stdout.log")
    stderr_path = Path(output_dir) / (stem + ".stderr.log")
    stdout_path.write_text(result["stdout"], encoding="utf-8")
    stderr_path.write_text(result["stderr"], encoding="utf-8")
    return {
        "stdout_log": stdout_path.name,
        "stderr_log": stderr_path.name,
    }


def tool_inventory(kafka_dir, tools):
    inventory = {}
    for tool in tools:
        path = Path(kafka_dir) / "bin" / tool
        present = path.is_file()
        inventory[tool] = {
            "present": present,
            "executable": present and os.access(path, os.X_OK),
        }
    return inventory


def probe(name, mandatory, status, category, evidence, limitation=None):
    item = {
        "name": name,
        "mandatory": mandatory,
        "status": status,
        "category": category,
        "evidence": evidence,
    }
    if limitation:
        item["limitation"] = limitation
    return item


def base_probes(
    kafka_dir,
    bootstrap,
    timeout,
    client_config,
    required_tools,
):
    """Check CLI/runtime interfaces and authenticated metadata access."""
    probes = []
    inventory = tool_inventory(kafka_dir, required_tools)
    missing = [
        name
        for name, item in inventory.items()
        if not item["present"] or not item["executable"]
    ]
    probes.append(
        probe(
            "official_cli_tools",
            True,
            "blocked" if missing else "ready",
            "absent" if missing else "ok",
            {"tools": inventory, "missing_or_not_executable": missing},
        )
    )

    version_result = run_cli(
        kafka_dir,
        "kafka-topics.sh",
        ["--version"],
        timeout,
        client_config,
    )
    version_match = re.search(
        r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)",
        version_result["stdout"],
    )
    version = (
        tuple(int(value) for value in version_match.groups())
        if version_match
        else None
    )
    category = error_category(version_result)
    if category is None and version is None:
        category = "error"
    if category is None and version[:2] not in SUPPORTED_VERSIONS:
        category = "wrong_version"
    probes.append(
        probe(
            "cli_version",
            True,
            "blocked" if category else "ready",
            category or "ok",
            {
                "command": public_command(version_result),
                "version": ".".join(str(value) for value in version)
                if version
                else None,
                "supported_interface_profiles": [
                    f"{major}.{minor}.x" for major, minor in SUPPORTED_VERSIONS
                ],
                "diagnostic": version_result["stderr"][-1000:],
            },
            "Only the explicitly listed CLI argument surfaces are accepted.",
        )
    )

    java_home = os.environ.get("JAVA_HOME")
    java_command = str(Path(java_home) / "bin/java") if java_home else "java"
    java_result = run_process([java_command, "-version"], timeout)
    java_category = error_category(java_result)
    java_text = java_result["stderr"] + "\n" + java_result["stdout"]
    java_match = re.search(r'version\s+"([^"]+)"', java_text)
    java_version = java_match.group(1) if java_match else None
    java_major = None
    if java_version:
        first = java_version.split(".", 1)[0]
        if first == "1":
            parts = java_version.split(".")
            if len(parts) > 1 and parts[1].isdigit():
                java_major = int(parts[1])
        elif first.isdigit():
            java_major = int(first)
    if java_category is None and java_version is None:
        java_category = "error"
    if java_category is None and (
        java_major is None or java_major < MINIMUM_JAVA_MAJOR
    ):
        java_category = "wrong_version"
    probes.append(
        probe(
            "java_runtime",
            True,
            "blocked" if java_category else "ready",
            java_category or "ok",
            {
                "command": public_command(java_result),
                "version": java_version,
                "major": java_major,
                "minimum_major": MINIMUM_JAVA_MAJOR,
                "selected_from": "JAVA_HOME" if java_home else "PATH",
                "diagnostic": java_result["stderr"][-1000:],
            },
        )
    )

    metadata_result = run_cli(
        kafka_dir,
        "kafka-topics.sh",
        ["--bootstrap-server", bootstrap, "--list"],
        timeout,
        client_config,
        "--command-config",
    )
    metadata_category = error_category(metadata_result)
    topic_count = (
        len([line for line in metadata_result["stdout"].splitlines() if line])
        if metadata_category is None
        else None
    )
    probes.append(
        probe(
            "authenticated_metadata",
            True,
            "blocked" if metadata_category else "ready",
            metadata_category or "ok",
            {
                "command": public_command(metadata_result),
                "metadata_entry_count": topic_count,
                "topic_names": "not_collected",
                "diagnostic": metadata_result["stderr"][-1000:],
            },
            "A metadata query proves more than socket reachability, but not "
            "produce/create/delete ACLs or production health.",
        )
    )
    return probes


def overall_status(probes):
    mandatory = [item for item in probes if item["mandatory"]]
    if any(item["status"] == "blocked" for item in mandatory):
        return "blocked"
    if any(item["status"] != "ready" for item in mandatory):
        return "unknown"
    return "ready"
