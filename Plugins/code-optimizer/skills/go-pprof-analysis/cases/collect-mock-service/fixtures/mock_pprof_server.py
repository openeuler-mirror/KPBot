#!/usr/bin/env python3
"""mock /debug/pprof/ HTTP 服务（python3 标准库），返回固定 payload 供采集脚本测试。"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PAYLOADS = {
    "/debug/pprof/profile": b"FAKE-CPU-PROFILE",
    "/debug/pprof/heap": b"FAKE-HEAP-PROFILE",
    "/debug/pprof/allocs": b"FAKE-ALLOCS-PROFILE",
    "/debug/pprof/goroutine": b"goroutine profile: total 2\nFAKE-GOROUTINE-STACK\n",
    "/debug/pprof/trace": b"FAKE-TRACE",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/debug/pprof/" or path == "/debug/pprof":
            self._send(b"pprof index\n")
        elif path in PAYLOADS:
            self._send(PAYLOADS[path])
        else:
            self._send(b"not found\n", 404)

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
