"""Bounded uploads with local TLS fixtures, without a Doris cluster."""

import errno
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/stream_load_https.py"
spec = importlib.util.spec_from_file_location("stream_load_https", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CHUNK = module.UPLOAD_CHUNK_SIZE
OBSERVATIONS = {}


class TrackingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_PUT(self):
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, TypeError, ValueError):
            self.send_error(400, "missing or invalid Content-Length")
            return
        record = {
            "path": self.path,
            "length": length,
            "auth": self.headers.get("Authorization"),
            "expect": self.headers.get("Expect"),
            "sha256": None,
            "read_lengths": [],
        }
        if not self.server.redirect:
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                part = self.rfile.read(min(CHUNK, remaining))
                if not part:
                    self.send_error(400, "truncated request body")
                    return
                record["read_lengths"].append(len(part))
                digest.update(part)
                remaining -= len(part)
            record["sha256"] = digest.hexdigest()
        self.server.requests.append(record)
        self.server.responder(self)


def respond(handler, status, headers, payload):
    handler.send_response(status)
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    if payload:
        handler.wfile.write(payload)


class SecureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_cert(directory, name):
    cert = directory / f"{name}.crt"
    key = directory / f"{name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def write_pattern_file(path, size):
    digest = hashlib.sha256()
    block = (b"0123456789abcdef|kpbot-review-stream|\n" * 2048)[:CHUNK]
    with path.open("wb") as stream:
        remaining = size
        while remaining:
            part = block[: min(len(block), remaining)]
            stream.write(part)
            digest.update(part)
            remaining -= len(part)
    return digest.hexdigest()


class FakeConnection:
    def __init__(self):
        self.sends = []
        self.closed = False

    def send(self, data):
        self.sends.append(bytes(data))

    def close(self):
        self.closed = True


class StreamingContractTests(unittest.TestCase):
    def test_upload_never_requests_unbounded_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.tbl"
            data = b"x" * (64 * 1024 * 3 + 7)
            path.write_bytes(data)
            stream, signature = module.open_regular_file(path)
            requested = []
            parent = self

            class BoundedReader:
                def fileno(self):
                    return stream.fileno()

                def seek(self, *args):
                    return stream.seek(*args)

                def read(self, size=-1):
                    parent.assertGreater(size, 0)
                    parent.assertLessEqual(size, 64 * 1024)
                    requested.append(size)
                    return stream.read(size)

            try:
                connection = FakeConnection()
                module.stream_file(
                    connection, BoundedReader(), path, signature, len(data)
                )
                self.assertEqual(b"".join(connection.sends), data)
                self.assertGreater(len(requested), 1)
                OBSERVATIONS["max_read_request_bytes"] = max(requested)
            finally:
                stream.close()

    def test_open_descriptor_must_match_the_named_file(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.tbl"
            second = Path(directory) / "second.tbl"
            first.write_bytes(b"same-size")
            second.write_bytes(b"different")
            original, signature = module.open_regular_file(first)
            original.close()
            with second.open("rb") as other:
                with self.assertRaises(module.LoadError):
                    module.ensure_same_file(other, first, signature)

    def test_disconnect_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.tbl"
            path.write_bytes(b"data")
            stream, signature = module.open_regular_file(path)
            connection = Mock()
            connection.send.side_effect = BrokenPipeError(
                errno.EPIPE, "closed early"
            )
            connection.getresponse.return_value.status = 200
            upload = {
                "stream": stream,
                "path": path,
                "signature": signature,
                "size": 4,
            }
            try:
                with patch.object(
                    module.http.client,
                    "HTTPSConnection",
                    return_value=connection,
                ):
                    with self.assertRaises(module.LoadError):
                        module.send(
                            "https://localhost/load",
                            upload,
                            {},
                            "test-auth",
                            object(),
                            1,
                        )
                connection.close.assert_called_once()
            finally:
                stream.close()

    def test_non_regular_data_file_is_rejected(self):
        args = type(
            "Args",
            (),
            {
                "expected_rows": 1,
                "allow_host": ["127.0.0.1"],
                "password_env": "DORIS_PASSWORD",
                "data_file": Path(os.devnull),
                "cafile": None,
                "client_cert": None,
                "client_key": None,
                "header": [],
                "user": "root",
                "fe_url": "https://127.0.0.1/api/db/tbl/_stream_load",
                "timeout": 1.0,
                "max_redirects": 0,
                "be_https_port": None,
            },
        )()
        with patch.dict(os.environ, {"DORIS_PASSWORD": "secret"}, clear=False):
            with self.assertRaises(module.LoadError) as caught:
                module.load(args)
        self.assertIn("regular file", str(caught.exception))

    def test_stream_file_sends_bounded_complete_chunks_and_detects_change(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.tbl"
            expected = (b"0123456789abcdef" * 5000) + b"tail"
            path.write_bytes(expected)
            stream, signature = module.open_regular_file(path)
            try:
                connection = FakeConnection()
                module.stream_file(
                    connection, stream, path, signature, len(expected)
                )
            finally:
                stream.close()
            self.assertTrue(
                all(len(item) <= CHUNK for item in connection.sends)
            )
            self.assertGreater(len(connection.sends), 1)
            self.assertEqual(b"".join(connection.sends), expected)

            stream, signature = module.open_regular_file(path)
            try:
                path.write_bytes(b"changed")
                with self.assertRaises(module.LoadError) as caught:
                    module.stream_file(
                        connection,
                        stream,
                        path,
                        signature,
                        len(expected),
                    )
                self.assertIn("changed", str(caught.exception))
            finally:
                stream.close()


class LocalTlsStreamingFixture(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required for the local HTTPS fixture")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ca_cert, self.ca_key = make_cert(self.root, "ca")
        self.data_file = self.root / "large-lineitem.tbl"
        self.expected_sha256 = write_pattern_file(
            self.data_file, 8 * 1024 * 1024 + 123
        )
        self.servers = []
        self.middle = self.start_server("middle", True)
        self.first = self.start_server("first", True)
        self.final = self.start_server("final", False)
        self.fe = self.start_server("fe", True)
        self.first.location = (
            f"https://root:secret@127.0.0.1:{self.middle.server_port}"
            "/api/test/lineitem/_stream_load?token=secret"
        )
        self.middle.location = (
            f"https://127.0.0.1:{self.final.server_port}"
            "/api/test/lineitem/_stream_load"
        )
        self.fe.location = (
            f"https://root:secret@127.0.0.1:{self.first.server_port}"
            "/api/test/lineitem/_stream_load"
        )
        self.addCleanup(self.stop_servers)

    def start_server(self, role, redirect):
        server = SecureServer(("127.0.0.1", 0), TrackingHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.ca_cert), str(self.ca_key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.requests = []
        server.role = role
        server.redirect = redirect
        server.location = None

        def responder(handler):
            if server.redirect:
                respond(handler, 307, {"Location": server.location}, b"")
                return
            respond(
                handler,
                200,
                {"Content-Type": "application/json"},
                json.dumps(
                    {
                        "Status": "Success",
                        "NumberLoadedRows": 1,
                        "NumberFilteredRows": 0,
                    }
                ).encode(),
            )

        server.responder = responder
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return server

    def stop_servers(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def args(self, **overrides):
        values = {
            "fe_url": (
                f"https://127.0.0.1:{self.fe.server_port}"
                "/api/test/lineitem/_stream_load"
            ),
            "data_file": self.data_file,
            "allow_host": ["127.0.0.1"],
            "be_https_port": None,
            "cafile": self.ca_cert,
            "client_cert": None,
            "client_key": None,
            "user": "root",
            "password_env": "DORIS_PASSWORD",
            "header": [],
            "expected_rows": 1,
            "timeout": 30.0,
            "max_redirects": 4,
        }
        values.update(overrides)
        options = [
            "--fe-url",
            str(values["fe_url"]),
            "--data-file",
            str(values["data_file"]),
            "--allow-host",
            *values["allow_host"],
            "--cafile",
            str(values["cafile"]),
            "--expected-rows",
            str(values["expected_rows"]),
            "--timeout",
            str(values["timeout"]),
            "--max-redirects",
            str(values["max_redirects"]),
        ]
        if values["be_https_port"] is not None:
            options += ["--be-https-port", str(values["be_https_port"])]
        return module.parser().parse_args(options)

    def load(self, **overrides):
        args = self.args(**overrides)
        with patch.dict(os.environ, {"DORIS_PASSWORD": "secret"}, clear=False):
            return module.load(args)

    def test_sizable_tls_upload_replays_complete_body_and_bounds_memory(self):
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = self.load()
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertEqual(result["status"], "passed")
        expected_size = self.data_file.stat().st_size
        self.assertEqual(len(self.fe.requests), 1)
        discovery = self.fe.requests[0]
        self.assertEqual(discovery["length"], 0)
        self.assertIsNone(discovery["sha256"])
        self.assertEqual(discovery["read_lengths"], [])
        self.assertEqual(discovery["expect"], "100-continue")
        self.assertTrue(discovery["auth"].startswith("Basic "))
        for server in (self.first, self.middle):
            self.assertEqual(len(server.requests), 1, server.role)
            request = server.requests[0]
            self.assertEqual(request["length"], expected_size)
            self.assertIsNone(request["sha256"])
            self.assertEqual(request["read_lengths"], [])
            self.assertEqual(request["expect"], "100-continue")
            self.assertTrue(request["auth"].startswith("Basic "))
        self.assertEqual(len(self.final.requests), 1)
        final = self.final.requests[0]
        self.assertEqual(final["length"], expected_size)
        self.assertEqual(final["sha256"], self.expected_sha256)
        self.assertGreater(len(final["read_lengths"]), 1)
        self.assertEqual(final["expect"], "100-continue")
        rss_delta_bytes = max(0, after - before) * 1024
        self.assertLess(rss_delta_bytes, 32 * 1024 * 1024)
        OBSERVATIONS.update(
            tls_payload_bytes=expected_size,
            fe_payload_bytes=0,
            tls_body_sha256_matches=True,
            tls_read_chunks=len(final["read_lengths"]),
            process_peak_rss_delta_bytes=rss_delta_bytes,
            rss_scope="Linux process peak, including local TLS fixtures",
        )

    def test_blocked_fe_redirect_receives_no_payload(self):
        with self.assertRaises(module.LoadError) as caught:
            self.load(allow_host=["other.example"])
        self.assertIn("not in --allow-host", str(caught.exception))
        self.assertEqual(len(self.fe.requests), 1)
        self.assertEqual(self.fe.requests[0]["length"], 0)
        self.assertEqual(self.first.requests, [])
        self.assertEqual(self.middle.requests, [])
        self.assertEqual(self.final.requests, [])

    def test_blocked_be_redirect_receives_no_payload(self):
        original = module.redirect_target
        calls = []

        def checked(current, headers, allowed, be_https_port=None):
            calls.append(current)
            if len(calls) == 2:
                raise module.LoadError(
                    "redirect host is not in --allow-host: blocked.example"
                )
            return original(current, headers, allowed, be_https_port)

        with patch.object(module, "redirect_target", side_effect=checked):
            with self.assertRaises(module.LoadError) as caught:
                self.load()
        self.assertIn("not in --allow-host", str(caught.exception))
        self.assertEqual(len(self.fe.requests), 1)
        self.assertEqual(self.fe.requests[0]["length"], 0)
        self.assertEqual(len(self.first.requests), 1)
        self.assertEqual(
            self.first.requests[0]["length"], self.data_file.stat().st_size
        )
        self.assertEqual(self.middle.requests, [])
        self.assertEqual(self.final.requests, [])
        self.assertEqual(len(calls), 2)

    def test_http_redirect_without_upgrade_port_sends_no_payload(self):
        self.fe.location = (
            "http://127.0.0.1:8040/api/test/lineitem/_stream_load"
        )
        with self.assertRaises(module.LoadError) as caught:
            self.load(be_https_port=None)
        self.assertIn("--be-https-port", str(caught.exception))
        self.assertEqual(len(self.fe.requests), 1)
        self.assertEqual(self.fe.requests[0]["length"], 0)
        self.assertEqual(self.first.requests, [])
        self.assertEqual(self.middle.requests, [])
        self.assertEqual(self.final.requests, [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skipped": len(result.skipped),
                "evidence": "bounded reads + local TLS; no Doris cluster",
                "observations": OBSERVATIONS,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
