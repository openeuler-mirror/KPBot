"""Loopback proxy and controller helpers shared by tests and evals."""
import contextlib
import http.client
import json
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capturectl


def config(path, **values):
    capturectl.write_config(path, {"out_dir": str(path.parent / "data"), **values})


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "capturectl.py"), *map(str, args)],
                          capture_output=True, text=True, timeout=15)


def wait_worker_count(path, expected, timeout=6):
    deadline = time.monotonic() + timeout
    while len(capturectl.active_state(path)["workers"]) != expected:
        if time.monotonic() >= deadline:
            raise AssertionError("expected workers did not publish their initial state")
        time.sleep(0.05)


@contextlib.contextmanager
def mock_process(*args):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests/mock_proxy.py"), *map(str, args)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Fail clearly rather than hanging if a mock fails to bind/start.
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready:
            raise AssertionError("mock startup timeout")
        line = proc.stdout.readline()
        if not line:
            raise AssertionError(proc.stderr.read())
        yield proc, int(line)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def request(port, stream=False, session="session-test"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = {"model": "mock", "stream": stream,
            "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
    try:
        connection.request("POST", "/v1/chat/completions", json.dumps(body),
                           {"x-session-id": session, "Authorization": "Bearer SECRET"})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()
