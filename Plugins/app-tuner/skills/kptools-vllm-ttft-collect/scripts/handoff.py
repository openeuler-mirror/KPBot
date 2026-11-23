"""Bind a TTFT request ID to a stable Prefill log snapshot."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


BREAKDOWN = re.compile(rb"\[TTFT\]\[BREAKDOWN\] req=(\S+)")
ORIGINS = ("live_request", "existing_logs")
FIELDS = {"schema_version", "origin", "request_id", "prefill_log", "sha256"}


def snapshot(path, request_id):
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Prefill log must be a regular file")
    request = request_id.encode("utf-8")
    digest = hashlib.sha256()
    breakdown_count = 0
    matched = False
    with path.open("rb") as log:
        for line in log:
            digest.update(line)
            breakdown_count += line.count(b"[TTFT][BREAKDOWN]")
            if not matched:
                matched = any(m[1] == request for m in BREAKDOWN.finditer(line))
    if not matched:
        raise ValueError("request_id has no BREAKDOWN evidence in the Prefill log")
    if breakdown_count != 1:
        raise ValueError("report requires exactly one BREAKDOWN; provide a single-request snapshot")
    return str(path), digest.hexdigest()


def read_receipt(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.keys() != FIELDS
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["origin"] not in ORIGINS
        or any(not isinstance(data[k], str) or not data[k] for k in FIELDS - {"schema_version"})
        or not Path(data["prefill_log"]).is_absolute()
        or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])
    ):
        raise ValueError("invalid TTFT handoff schema v1")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--log", type=Path, required=True)
    collect.add_argument("--request-id", required=True)
    collect.add_argument("--origin", choices=ORIGINS, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--log", type=Path, help="local copy of the original log snapshot")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            log, digest = snapshot(args.log, args.request_id)
            result = dict(schema_version=1, origin=args.origin, request_id=args.request_id,
                          prefill_log=log, sha256=digest)
        else:
            result = read_receipt(args.receipt)
            log, digest = snapshot(args.log or Path(result["prefill_log"]), result["request_id"])
            if digest != result["sha256"]:
                raise ValueError("Prefill log bytes no longer match the handoff")
            result["prefill_log"] = log
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
