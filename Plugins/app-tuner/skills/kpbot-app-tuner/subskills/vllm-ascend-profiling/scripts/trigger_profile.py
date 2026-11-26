#!/usr/bin/env python3
"""Trigger vLLM V1 profiling over HTTP: start / stop / run (3s window)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _post(url: str, payload: dict | None = None, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return e.code, body or e.reason
    except urllib.error.URLError as e:
        return -1, f"connection failed: {e.reason if hasattr(e, 'reason') else e}"


def cmd_start(api_base: str, timeout: float, payload: dict | None) -> int:
    status, body = _post(api_base.rstrip("/") + "/start_profile", payload, timeout)
    print(json.dumps({"action": "start", "http_status": status, "body": body[:500]}))
    return 0 if 0 <= status < 300 else 1


def cmd_stop(api_base: str, timeout: float, payload: dict | None) -> int:
    status, body = _post(api_base.rstrip("/") + "/stop_profile", payload, timeout)
    print(json.dumps({"action": "stop", "http_status": status, "body": body[:500]}))
    return 0 if 0 <= status < 300 else 1


def cmd_run(api_base: str, window: float, timeout: float, payload: dict | None) -> int:
    rc = cmd_start(api_base, timeout, payload)
    if rc != 0:
        print(
            json.dumps(
                {
                    "error": "start_profile failed; skip window",
                    "hint": f"default port is 7100 ({api_base}); if unreachable, "
                    "ask the user for the correct port and pass --api-base",
                }
            ),
            file=sys.stderr,
        )
        return rc
    time.sleep(window)
    return cmd_stop(api_base, timeout, payload)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["start", "stop", "run"])
    ap.add_argument("--api-base", default="http://127.0.0.1:7100")
    ap.add_argument("--window", type=float, default=3.0, help="collection window seconds for `run`")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument(
        "--payload-json",
        default="{}",
        help='extra JSON body for the profile API, e.g. \'{"activities": ["CPU", "NPU"]}\'',
    )
    args = ap.parse_args()
    try:
        payload = json.loads(args.payload_json) if args.payload_json else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid --payload-json: {e}"}), file=sys.stderr)
        return 2

    if args.action == "start":
        return cmd_start(args.api_base, args.timeout, payload)
    if args.action == "stop":
        return cmd_stop(args.api_base, args.timeout, payload)
    return cmd_run(args.api_base, args.window, args.timeout, payload)


if __name__ == "__main__":
    sys.exit(main())
