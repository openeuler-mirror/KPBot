"""Verify Kafka message delivery with the official Kafka CLI (Linux + Bash)."""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be between 0 and 3600 seconds"
        )
    return seconds


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kafka-dir", type=Path, required=True)
    result.add_argument("--bootstrap", default="localhost:9092")
    result.add_argument(
        "--client-config",
        type=Path,
        help="shared admin/producer/consumer properties",
    )
    result.add_argument("--timeout", type=positive_timeout, default=30)
    result.add_argument("--output", type=Path, required=True)
    return result


def check(args):
    args.output.mkdir(parents=True, exist_ok=False)
    topic = "kpbot-smoke-" + uuid.uuid4().hex
    result = {
        "service": "kafka",
        "status": "failed",
        "bootstrap": args.bootstrap,
        "topic": topic,
        "checks": [],
        "cleanup": "topic retained",
    }

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
        tool(
            "kafka-topics.sh",
            [
                "--create",
                "--topic",
                topic,
                "--partitions",
                "1",
                "--replication-factor",
                "1",
            ],
            config_flag="--command-config",
        )
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
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        (args.output / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    return result


def main():
    args = parser().parse_args()
    try:
        result = check(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
