#!/usr/bin/env python3
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# Licensed under Mulan PSL v2.
"""Read-only WeKnora Wiki client, using the Python standard library."""

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_MCP_URL = (
    "http://121.41.201.129/mcp/c044bfb0-84ae-465d-9eb2-b932ffd82b13"
)


class ClientError(Exception):
    """An actionable error safe to print without credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, token, timeout):
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except ValueError:
            raise ClientError("Invalid WEKNORA_MCP_URL") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ClientError("WEKNORA_MCP_URL must be a complete HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ClientError("URL must not contain credentials or a fragment")
        self.url, self.token, self.timeout = url, token, timeout
        self.session = None
        self.protocol = None
        self.counter = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, params=None, notification=False):
        self.counter += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.counter
        if params is not None:
            payload["params"] = params
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        try:
            req = urllib.request.Request(
                self.url, json.dumps(payload).encode("utf-8"), headers
            )
            with self.opener.open(req, timeout=self.timeout) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification:
                    return None
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    data = []
                    for raw in response:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        elif not line and data:
                            message = json.loads("\n".join(data))
                            data = []
                            if message.get("id") == payload["id"]:
                                return self.result(message)
                    raise ClientError("SSE stream ended without a matching response")
                message = json.load(response)
                if message.get("id") != payload["id"]:
                    raise ClientError("Mismatched JSON-RPC response id")
                return self.result(message)
        except urllib.error.HTTPError as exc:
            raise ClientError("MCP HTTP error: " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException):
            raise ClientError("MCP connection failed or timed out") from None
        except (ValueError, UnicodeError, AttributeError, TypeError):
            raise ClientError("Invalid MCP response") from None

    @staticmethod
    def result(message):
        if "error" in message:
            error = message["error"]
            raise ClientError(
                "MCP JSON-RPC error: " + str(error.get("code", "unknown"))
                + " " + str(error.get("message", ""))[:300]
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise ClientError("Missing MCP result")
        if result.get("isError"):
            details = "; ".join(
                item.get("text", "") for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            )
            raise ClientError("MCP tool reported an error: " + details[:300])
        return result

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "kpbot-kunpeng-knowledge", "version": "1.0.0"
            },
        })
        self.protocol = result.get("protocolVersion")
        if self.protocol not in (
            "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"
        ):
            raise ClientError("Unsupported MCP protocol version")
        self.request("notifications/initialized", notification=True)


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=positive, default=30,
        help="seconds per HTTP operation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--regex", action="store_true")
    search.add_argument("--kb", action="append")
    search.add_argument("--limit", type=positive, default=10)
    read = commands.add_parser("read")
    read.add_argument("slug")
    read.add_argument("--kb")
    index = commands.add_parser("index")
    index.add_argument("--kb", required=True)
    index.add_argument("--limit", type=positive, default=50)
    args = parser.parse_args()
    if args.command == "search":
        if args.limit > 50:
            parser.error("search --limit must be at most 50")
        tool = "wiki_search"
        arguments = {"query": args.query, "regex": args.regex, "limit": args.limit}
        if args.kb:
            arguments["knowledge_base_ids"] = args.kb
    elif args.command == "read":
        tool, arguments = "wiki_read_page", {"slug": args.slug}
        if args.kb:
            arguments["knowledge_base_id"] = args.kb
    else:
        if args.limit > 200:
            parser.error("index --limit must be at most 200")
        tool = "wiki_index"
        arguments = {"knowledge_base_id": args.kb, "limit": args.limit}
    token = os.environ.get("WEKNORA_MCP_TOKEN", "").strip()
    try:
        url = os.environ.get("WEKNORA_MCP_URL", "").strip() or DEFAULT_MCP_URL
        if not token:
            raise ClientError(
                "未配置 WEKNORA_MCP_TOKEN。请联系本技能的知识库维护者申请令牌，"
                "在本地环境变量中设置后重新启动 Agent；不要将令牌发送到聊天。"
            )
        client = Client(url, token, args.timeout)
        client.initialize()
        result = client.request("tools/call", {"name": tool, "arguments": arguments})
        output = json.dumps(result, ensure_ascii=False)
        # Never echo the configured credential, even if a server reflects it.
        print(output.replace(token, "[REDACTED]"))
        return 0
    except ClientError as exc:
        error = str(exc).replace(token, "[REDACTED]") if token else str(exc)
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
