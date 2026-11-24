"""HTTPS Stream Load tests with a local test CA and FE/BE mock servers."""

import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/stream_load_https.py"
spec = importlib.util.spec_from_file_location("stream_load_https", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization"),
                "label": self.headers.get("label"),
                "separator": self.headers.get("column_separator"),
                "expect": self.headers.get("Expect"),
            }
        )
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


class StreamLoadHttpsTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required for the local HTTPS mock")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ca_cert, self.ca_key = make_cert(self.root, "ca")
        self.other_cert, self.other_key = make_cert(self.root, "other")
        self.data = b"1|first\n2|second\n3|third\n"
        self.data_file = self.root / "region.tbl"
        self.data_file.write_bytes(self.data)
        self.servers = []
        self.be = self.start_be()
        self.fe = self.start_fe(self.be.server_port)
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def start_server(self, cert, key):
        server = SecureServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return server

    def start_be(self):
        server = self.start_server(self.ca_cert, self.ca_key)
        server.loaded = 3
        server.filtered = 0
        server.status = "Success"
        server.redirect = None
        server.extras = {
            "ErrorURL": (
                "http://root:secret@127.0.0.1:8040/api/_load_error_log"
                "?token=secret"
            )
        }

        def responder(handler):
            if server.redirect:
                respond(
                    handler,
                    307,
                    {"Location": server.redirect},
                    b"",
                )
                return
            value = {
                "Status": server.status,
                "Label": "123_region",
                "NumberLoadedRows": server.loaded,
                "NumberFilteredRows": server.filtered,
            }
            value.update(server.extras)
            payload = json.dumps(value).encode()
            respond(
                handler,
                200,
                {"Content-Type": "application/json"},
                payload,
            )

        server.responder = responder
        return server

    def start_fe(self, be_port):
        server = self.start_server(self.ca_cert, self.ca_key)
        location = (
            "http://root:secret@127.0.0.1:8040"
            "/api/test/region/_stream_load"
        )
        server.location = location

        def responder(handler):
            respond(handler, 307, {"Location": server.location}, b"")

        server.responder = responder
        return server

    def args(self, **overrides):
        values = {
            "fe_url": (
                f"https://127.0.0.1:{self.fe.server_port}"
                "/api/test/region/_stream_load"
            ),
            "data_file": self.data_file,
            "allow_host": ["127.0.0.1"],
            "be_https_port": self.be.server_port,
            "cafile": self.ca_cert,
            "client_cert": None,
            "client_key": None,
            "user": None,
            "password_env": "DORIS_PASSWORD",
            "header": [("column_separator", "|")],
            "expected_rows": 3,
            "timeout": 10.0,
            "max_redirects": 2,
            "output": None,
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
        for name, value in values["header"]:
            options += ["--header", f"{name}:{value}"]
        if values["be_https_port"] is not None:
            options += ["--be-https-port", str(values["be_https_port"])]
        if values["user"] is not None:
            options += ["--user", values["user"]]
        return module.parser().parse_args(options)

    def load(self, **overrides):
        args = self.args(**overrides)
        with patch.dict(os.environ, {"DORIS_PASSWORD": "secret"}, clear=False):
            return module.load(args)

    def test_fe_redirect_is_validated_before_data_and_credentials(self):
        result = self.load()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["tls_verified"])
        self.assertTrue(result["redirect_validated"])
        self.assertEqual(len(self.fe.requests), 1)
        self.assertEqual(self.fe.requests[0]["body"], b"")
        self.assertTrue(self.fe.requests[0]["auth"].startswith("Basic "))
        self.assertEqual(self.fe.requests[0]["expect"], "100-continue")
        self.assertEqual(len(self.be.requests), 1)
        self.assertEqual(self.be.requests[0]["body"], self.data)
        self.assertTrue(self.be.requests[0]["auth"].startswith("Basic "))
        self.assertEqual(self.be.requests[0]["expect"], "100-continue")
        self.assertEqual(
            result["be_url"],
            f"https://127.0.0.1:{self.be.server_port}"
            "/api/test/region/_stream_load",
        )
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("root@", json.dumps(result))

    def test_explicit_user_is_used_for_basic_auth(self):
        result = self.load(user="benchmark")
        self.assertEqual(result["status"], "passed")
        expected = module.basic_auth("benchmark", "secret")
        self.assertEqual(self.fe.requests[0]["auth"], expected)
        self.assertEqual(self.be.requests[0]["auth"], expected)

    def test_plaintext_redirect_requires_a_configured_tls_port(self):
        with self.assertRaises(module.LoadError) as caught:
            self.load(be_https_port=None)
        self.assertIn("--be-https-port", str(caught.exception))
        self.assertEqual(self.be.requests, [])

    def test_native_https_redirect_does_not_require_rewrite_port(self):
        self.fe.location = (
            f"https://127.0.0.1:{self.be.server_port}"
            "/api/test/region/_stream_load"
        )
        result = self.load(be_https_port=None)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["be_url"], self.fe.location)

    def test_explicit_label_header_is_sent_to_be(self):
        result = self.load(
            header=[
                ("column_separator", "|"),
                ("label", "tpch-1_region_0"),
            ]
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.be.requests[0]["label"], "tpch-1_region_0")
        self.assertEqual(self.be.requests[0]["separator"], "|")

    def test_redirect_to_unlisted_host_is_rejected_before_data(self):
        with self.assertRaises(module.LoadError) as caught:
            self.load(allow_host=["other.example"])
        self.assertIn("not in --allow-host", str(caught.exception))
        self.assertEqual(self.be.requests, [])

    def test_untrusted_certificate_stops_before_fe_request(self):
        with self.assertRaises(ssl.SSLError):
            self.load(cafile=self.other_cert)
        self.assertEqual(self.fe.requests, [])
        self.assertEqual(self.be.requests, [])

    def test_plain_http_url_is_rejected(self):
        with self.assertRaises(module.LoadError) as caught:
            self.load(fe_url="http://127.0.0.1:8030/api/x/y/_stream_load")
        self.assertIn("must use https", str(caught.exception))
        self.assertEqual(self.fe.requests, [])

    def test_non_success_status_is_rejected(self):
        self.be.status = "Fail"
        with self.assertRaises(module.LoadError) as caught:
            self.load()
        self.assertIn("Status is not Success", str(caught.exception))

    def test_filtered_rows_are_rejected(self):
        self.be.filtered = 1
        with self.assertRaises(module.LoadError) as caught:
            self.load()
        self.assertIn("filtered 1 rows", str(caught.exception))

    def test_loaded_row_mismatch_is_rejected(self):
        self.be.loaded = 2
        with self.assertRaises(module.LoadError) as caught:
            self.load()
        self.assertIn("imported 2 rows", str(caught.exception))

    def test_be_redirect_to_unlisted_host_is_rejected(self):
        self.be.redirect = "https://198.51.100.10/stream"
        with self.assertRaises(module.LoadError) as caught:
            self.load()
        self.assertIn("not in --allow-host", str(caught.exception))
        self.assertEqual(len(self.be.requests), 1)

    def test_business_failures_save_sanitized_response_before_nonzero(self):
        scenarios = (
            ("status", "status", "Fail"),
            ("filtered", "filtered", 1),
            ("loaded", "loaded", 2),
        )
        for name, attribute, value in scenarios:
            with self.subTest(failure=name):
                self.be.status = "Success"
                self.be.filtered = 0
                self.be.loaded = 3
                setattr(self.be, attribute, value)
                output = self.root / f"failed-{name}"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--fe-url",
                        f"https://127.0.0.1:{self.fe.server_port}"
                        "/api/test/region/_stream_load",
                        "--data-file",
                        str(self.data_file),
                        "--allow-host",
                        "127.0.0.1",
                        "--be-https-port",
                        str(self.be.server_port),
                        "--cafile",
                        str(self.ca_cert),
                        "--expected-rows",
                        "3",
                        "--output",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    env=dict(os.environ, DORIS_PASSWORD="secret"),
                    timeout=30,
                )
                self.assertEqual(completed.returncode, 1, completed.stderr)
                evidence = output / "stream-load-response.json"
                self.assertTrue(evidence.is_file())
                text = evidence.read_text(encoding="utf-8")
                response = json.loads(text)
                self.assertEqual(response["Status"], self.be.status)
                self.assertNotIn("secret", text)
                self.assertIn("[redacted]", text)
                result = json.loads(
                    (output / "result.json").read_text(encoding="utf-8")
                )
                self.assertEqual(result["status"], "failed")

    def test_cli_writes_fresh_evidence_and_rejects_existing_output(self):
        output = self.root / "evidence"
        args = [
            sys.executable,
            str(SCRIPT),
            "--fe-url",
            f"https://127.0.0.1:{self.fe.server_port}/api/test/region/_stream_load",
            "--data-file",
            str(self.data_file),
            "--allow-host",
            "127.0.0.1",
            "--be-https-port",
            str(self.be.server_port),
            "--user",
            "benchmark",
            "--cafile",
            str(self.ca_cert),
            "--expected-rows",
            "3",
            "--output",
            str(output),
        ]
        env = dict(os.environ, DORIS_PASSWORD="secret")
        first = subprocess.run(
            args, capture_output=True, text=True, env=env, timeout=30
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["status"], "passed")
        self.assertTrue((output / "stream-load-response.json").is_file())
        self.assertNotIn(
            "secret",
            (output / "stream-load-response.json").read_text(encoding="utf-8"),
        )
        second = subprocess.run(
            args, capture_output=True, text=True, env=env, timeout=30
        )
        self.assertEqual(second.returncode, 2)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parent / "results"
    output.mkdir(exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(
            {
                "success": result.wasSuccessful(),
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.wasSuccessful() else 1)
