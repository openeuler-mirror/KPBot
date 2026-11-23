"""Check doris using its official client and retain raw evidence."""

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


def alive_rows(text, required):
    lines = [line.split("\t") for line in text.strip().splitlines()]
    if len(lines) < 2:
        raise ValueError("cluster status contains no nodes")
    indexes = [lines[0].index(field) for field in required]
    if any(
        index >= len(row) or row[index].lower() not in ("true", "1")
        for row in lines[1:]
        for index in indexes
    ):
        raise ValueError("cluster node is not ready")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="localhost")
    result.add_argument("--port", type=int, default=9030)
    result.add_argument("--user", default="root")
    result.add_argument("--client", default="mysql")
    result.add_argument(
        "--protocol",
        default="TCP",
        choices=("TCP", "SOCKET", "PIPE", "MEMORY"),
        help="MySQL connection protocol; use TCP so --port is honored",
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
        "service": "doris",
        "status": "failed",
        "host": args.host,
        "port": args.port,
        "checks": [],
    }

    def query(sql):
        argv = [args.client]
        if args.config:
            argv += [f"--defaults-extra-file={args.config}"]
        argv += [
            f"--protocol={args.protocol}",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--user",
            args.user,
        ]
        argv += ["--batch", "--raw", "--execute", sql]
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
        # Keep column names for cluster-status validation.
        one = query("SELECT 1").strip().splitlines()
        if not one or one[-1] != "1":
            raise ValueError("SELECT 1 returned an unexpected result")
        version = query("SELECT version()").strip()
        if not version:
            raise ValueError("SELECT version() returned an empty result")
        result["version_output"] = version
        alive_rows(query("SHOW FRONTENDS"), ("Join", "Alive"))
        alive_rows(query("SHOW BACKENDS"), ("Alive",))
        sql = (
            "SELECT SUM(v) FROM (SELECT 1 AS v "
            "UNION ALL SELECT 2 UNION ALL SELECT 3) AS t"
        )
        values = query(sql).strip().splitlines()
        expected = "6"
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
