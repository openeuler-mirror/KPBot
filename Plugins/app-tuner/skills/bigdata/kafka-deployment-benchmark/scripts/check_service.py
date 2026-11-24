"""Verify Kafka message delivery with the official Kafka CLI (Linux + Bash)."""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import string
import subprocess
import sys
import uuid


class ConfigError(ValueError):
    """Raised when --client-config cannot be used without joining a group."""


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be between 0 and 3600 seconds"
        )
    return seconds


def positive_replication_factor(value):
    try:
        factor = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "replication factor must be a positive integer"
        )
    if factor < 1:
        raise argparse.ArgumentTypeError(
            "replication factor must be a positive integer"
        )
    return factor


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kafka-dir", type=Path, required=True)
    result.add_argument("--bootstrap", default="localhost:9092")
    result.add_argument(
        "--client-config",
        type=Path,
        help=(
            "shared admin/producer/consumer properties; must not contain "
            "group.id"
        ),
    )
    result.add_argument("--timeout", type=positive_timeout, default=30)
    result.add_argument(
        "--replication-factor",
        type=positive_replication_factor,
        default=None,
        help=(
            "test-topic replication factor; omit to inherit the broker "
            "default, pass 1 only for a single-node cluster"
        ),
    )
    result.add_argument("--output", type=Path, required=True)
    return result


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
                    "--client-config contains an invalid \\u escape near "
                    + repr(text[index - 1 : index + 5])
                )
            result.append(chr(int(digits, 16)))
            index += 5
            continue
        result.append(
            {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(char, char)
        )
        index += 1
    return "".join(result)


def _java_physical_lines(text):
    # Java Properties treats only CR, LF and CRLF as line terminators.
    # Form feed is whitespace, not a terminator, so splitlines() is wrong.
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            yield text[start:index]
            index += 1
            if index < length and text[index] == "\n":
                index += 1
            start = index
        elif char == "\n":
            yield text[start:index]
            index += 1
            start = index
        else:
            index += 1
    if start < length:
        yield text[start:]


def _java_logical_lines(text):
    if text.startswith("\ufeff"):
        text = text[1:]
    buffer = ""
    continued = False
    for line in _java_physical_lines(text):
        line = line.lstrip(" \t\f")
        if not continued:
            # Java ignores a comment or blank natural line as a whole,
            # including a trailing backslash. It never starts a continuation.
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
    """Parse the subset of Java .properties used by Kafka client configs."""
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


def load_client_properties(path):
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")
    except OSError as exc:
        raise ConfigError(
            f"cannot read --client-config {path}: {exc}"
        ) from exc
    return java_properties(text)


def check(args):
    args.output.mkdir(parents=True, exist_ok=False)
    topic = "kpbot-smoke-" + uuid.uuid4().hex
    consumer_group = "kpbot-smoke-" + uuid.uuid4().hex
    result = {
        "service": "kafka",
        "status": "failed",
        "bootstrap": args.bootstrap,
        "topic": topic,
        "consumer_group": consumer_group,
        "replication_factor": args.replication_factor,
        "checks": [],
        "cleanup": "no topic created",
    }

    def write_result():
        (args.output / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )

    def tool(name, parameters, data=None, config_flag=None):
        script = args.kafka_dir / "bin" / name
        argv = [
            "bash",
            str(script),
            "--bootstrap-server",
            args.bootstrap,
        ] + parameters
        if args.client_config and config_flag:
            argv += [config_flag, str(args.client_config)]
        index = len(result["checks"])
        item = {"tool": name, "returncode": None}
        result["checks"].append(item)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(
                input=data, timeout=args.timeout
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout, stderr = process.communicate()
        (args.output / f"{index:02d}.stdout").write_text(
            stdout, encoding="utf-8"
        )
        (args.output / f"{index:02d}.stderr").write_text(
            stderr, encoding="utf-8"
        )
        item["returncode"] = process.returncode
        if timed_out or process.returncode:
            raise ValueError(
                f"{name}: "
                + ("timeout" if timed_out else f"exit {process.returncode}")
            )
        return stdout

    try:
        if args.client_config:
            configured = load_client_properties(args.client_config)
            if "group.id" in configured:
                raise ConfigError(
                    "--client-config contains group.id; refusing to join a "
                    "configured consumer group before creating any topic. "
                    "Use a connection/auth-only config without group.id; "
                    "check_service.py creates its own unique consumer group."
                )
        create = ["--create", "--topic", topic, "--partitions", "1"]
        if args.replication_factor is not None:
            create += ["--replication-factor", str(args.replication_factor)]
        result["cleanup"] = (
            "topic creation result unknown; verify topic before cleanup"
        )
        write_result()
        tool("kafka-topics.sh", create, config_flag="--command-config")
        result["cleanup"] = "topic retained"
        write_result()
        tool(
            "kafka-topics.sh",
            ["--describe", "--topic", topic],
            config_flag="--command-config",
        )
        messages = [f"{topic}:{i}" for i in range(3)]
        tool(
            "kafka-console-producer.sh",
            ["--topic", topic, "--producer-property", "acks=all"],
            "\n".join(messages) + "\n",
            "--producer.config",
        )
        actual = tool(
            "kafka-console-consumer.sh",
            [
                "--topic",
                topic,
                "--group",
                consumer_group,
                "--from-beginning",
                "--max-messages",
                "3",
                "--timeout-ms",
                str(max(1, int(args.timeout * 1000))),
            ],
            config_flag="--consumer.config",
        )
        if actual.splitlines() != messages:
            raise ValueError("consumed messages differ from sent messages")
        result["status"] = "passed"
    except ConfigError as exc:
        result["error"] = str(exc)
        result["exit_code"] = 2
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        write_result()
    return result


def main():
    args = parser().parse_args()
    try:
        result = check(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return result.get("exit_code", 0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    sys.exit(main())
