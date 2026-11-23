"""Check clickhouse using its official client and retain raw evidence."""

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be between 0 and 3600 seconds"
        )
    return seconds


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="localhost")
    result.add_argument("--port", type=int, default=9000)
    result.add_argument("--user", default="default")
    result.add_argument("--client", default="clickhouse-client")
    result.add_argument(
        "--single-binary",
        action="store_true",
        help="invoke --client as clickhouse client",
    )
    result.add_argument(
        "--config", type=Path, help="client authentication configuration file"
    )
    result.add_argument("--timeout", type=positive_timeout, default=30)
    result.add_argument("--output", type=Path, required=True)
    return result


def check(args):
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        "service": "clickhouse",
        "scope": "connection-and-sql-only",
        "status": "failed",
        "host": args.host,
        "port": args.port,
        "checks": [],
    }

    def query(sql):
        argv = [args.client]
        if args.single_binary:
            argv.append("client")
        if args.config:
            argv += ["--config-file", str(args.config)]
        argv += [
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--user",
            args.user,
        ]
        argv += ["--format", "TSV", "--query", sql]
        index = len(result["checks"])
        item = {"sql": sql, "returncode": None}
        result["checks"].append(item)
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            shell=False,
        )
        (args.output / f"{index:02d}.stdout").write_text(
            completed.stdout, encoding="utf-8"
        )
        (args.output / f"{index:02d}.stderr").write_text(
            completed.stderr, encoding="utf-8"
        )
        item["returncode"] = completed.returncode
        if completed.returncode:
            raise ValueError(
                f"client query failed with exit code {completed.returncode}"
            )
        return completed.stdout

    try:
        one = query("SELECT 1").strip().splitlines()
        if not one or one[-1] != "1":
            raise ValueError("SELECT 1 returned an unexpected result")
        result["version_output"] = query("SELECT version()").strip()
        sql = "SELECT sum(number) FROM numbers(1000)"
        values = query(sql).strip().splitlines()
        expected = "499500"
        if not values or values[-1] != expected:
            raise ValueError(
                "aggregate result does not match the expected value"
            )
        result["status"] = "passed"
    except subprocess.TimeoutExpired as exc:
        result["error"] = "client timed out"
        for name, value in (
            ("timeout.stdout", exc.stdout),
            ("timeout.stderr", exc.stderr),
        ):
            (args.output / name).write_bytes(
                value if isinstance(value, bytes) else (value or "").encode()
            )
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    finally:
        (args.output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def main():
    args = parser().parse_args()
    if not 0 < args.port <= 65535:
        parser().error("invalid port")
    try:
        result = check(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
