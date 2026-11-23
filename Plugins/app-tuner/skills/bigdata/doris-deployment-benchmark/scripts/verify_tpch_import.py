"""Verify TPC-H Stream Load responses and eight table counts before timing."""

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys

TABLES = (
    "region",
    "nation",
    "supplier",
    "part",
    "partsupp",
    "customer",
    "orders",
    "lineitem",
)
SF1 = {
    "region": 5,
    "nation": 25,
    "supplier": 10000,
    "part": 200000,
    "partsupp": 800000,
    "customer": 150000,
    "orders": 1500000,
    "lineitem": 6001215,
}
LABEL = re.compile(
    r"(?:^|_)(partsupp|supplier|nation|region|customer|orders|lineitem|part)"
    r"(?:_\d+)?$",
    re.IGNORECASE,
)


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be between 0 and 3600 seconds"
        )
    return seconds


def objects_in(text):
    decoder = json.JSONDecoder()
    found = []
    index = 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end


def table_for(label):
    if not isinstance(label, str):
        return None
    match = LABEL.search(label)
    return match.group(1).lower() if match else None


def count_lines(path):
    total = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += chunk.count(b"\n")
    return total


def expected_from_data(data_dir):
    if not data_dir.is_dir():
        raise ValueError(
            f"generated data directory does not exist: {data_dir}"
        )
    expected = {}
    for table in TABLES:
        files = [
            path for path in data_dir.glob(f"{table}.tbl*") if path.is_file()
        ]
        if not files:
            raise ValueError(f"no generated files for {table} in {data_dir}")
        expected[table] = sum(count_lines(path) for path in files)
    return expected


def collect(load_output, responses):
    payloads = []
    if load_output is not None:
        payloads.extend(objects_in(load_output.read_text(encoding="utf-8")))
    if responses is not None:
        files = (
            sorted(responses.rglob("*.json"))
            if responses.is_dir()
            else [responses]
        )
        for path in files:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "Status" in value:
                payloads.append(value)
    return payloads


def load_output(args):
    if args.data_dir:
        expected = expected_from_data(args.data_dir)
    elif args.expected_json:
        expected = json.loads(args.expected_json.read_text(encoding="utf-8"))
    else:
        expected = dict(SF1)
    if set(expected) != set(TABLES):
        raise ValueError(
            "expected counts must contain exactly the eight tables"
        )
    payloads = collect(args.load_output, args.responses)
    if not payloads:
        raise ValueError("no Stream Load responses found")
    stats = {
        table: {
            "responses": 0,
            "loaded": 0,
            "filtered": 0,
            "statuses": [],
        }
        for table in TABLES
    }
    errors = []
    for payload in payloads:
        table = table_for(payload.get("Label"))
        if table is None:
            errors.append(
                "cannot map response label: " + str(payload.get("Label"))
            )
            continue
        status = payload.get("Status")
        stats[table]["statuses"].append(status)
        if status != "Success":
            errors.append(f"{table} Status is {status}")
        try:
            filtered = int(payload["NumberFilteredRows"])
            loaded = int(payload["NumberLoadedRows"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{table} lacks row counts")
            continue
        stats[table]["responses"] += 1
        stats[table]["filtered"] += filtered
        stats[table]["loaded"] += loaded
    for table in TABLES:
        item = stats[table]
        if item["responses"] == 0:
            errors.append(f"{table} has no Stream Load response")
            continue
        if item["filtered"] != 0:
            errors.append(f"{table} filtered {item['filtered']} rows")
        if item["loaded"] != expected[table]:
            errors.append(
                f"{table} imported {item['loaded']} rows, "
                f"expected {expected[table]}"
            )
    return expected, stats, errors


def query_count(args, table):
    argv = [args.client]
    if args.config:
        argv.append(f"--defaults-extra-file={args.config}")
    argv.append(f"--protocol={args.protocol}")
    argv += [
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--user",
        args.user,
        "--database",
        args.database,
        "--batch",
        "--skip-column-names",
        "--execute",
        f"SELECT COUNT(*) FROM {table}",
    ]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        shell=False,
    )
    if completed.returncode:
        raise ValueError(
            f"COUNT(*) for {table} failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    lines = completed.stdout.strip().splitlines()
    if not lines or not lines[-1].strip().isdigit():
        raise ValueError(f"COUNT(*) for {table} returned no integer")
    return int(lines[-1].strip()), completed.stdout


def check(args):
    expected, stats, errors = load_output(args)
    checks = []
    for table in TABLES:
        item = stats[table]
        item["expected"] = expected[table]
        item["count"] = None
        error = {
            "table": table,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
        try:
            item["count"], output = query_count(args, table)
            error["returncode"] = 0
            error["stdout"] = output
            if item["count"] != expected[table]:
                errors.append(
                    f"{table} COUNT(*) is {item['count']}, "
                    f"expected {expected[table]}"
                )
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            error["stderr"] = str(exc)
        checks.append(error)
    status = "passed" if not errors else "failed"
    result = {
        "status": status,
        "tables": stats,
        "errors": errors,
        "checks": checks,
    }
    if args.output is not None:
        for index, item in enumerate(checks):
            table = item["table"]
            (args.output / f"{index:02d}-{table}.stdout").write_text(
                item["stdout"], encoding="utf-8"
            )
            (args.output / f"{index:02d}-{table}.stderr").write_text(
                item["stderr"], encoding="utf-8"
            )
        (args.output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--load-output", type=Path)
    result.add_argument("--responses", type=Path)
    expected = result.add_mutually_exclusive_group()
    expected.add_argument("--expected-json", type=Path)
    expected.add_argument(
        "--data-dir",
        type=Path,
        help="generated tpch-data directory; expected rows are counted from "
        "table.tbl and table.tbl.N files",
    )
    result.add_argument("--database", required=True)
    result.add_argument("--host", default="localhost")
    result.add_argument("--port", type=int, default=9030)
    result.add_argument("--user", default="root")
    result.add_argument("--client", default="mysql")
    result.add_argument(
        "--protocol",
        default="TCP",
        choices=("TCP", "SOCKET", "PIPE", "MEMORY"),
    )
    result.add_argument("--config", type=Path)
    result.add_argument("--timeout", type=positive_timeout, default=30)
    result.add_argument("--output", type=Path)
    return result


def main():
    args = parser().parse_args()
    if args.load_output is None and args.responses is None:
        parser().error("provide --load-output and/or --responses")
    if not 0 < args.port <= 65535:
        parser().error("invalid port")
    if args.output is not None:
        try:
            args.output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            print("output directory already exists", file=sys.stderr)
            return 2
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        result = check(args)
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "errors": [str(exc)]}
        if args.output is not None:
            (args.output / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
