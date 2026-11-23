"""Check an adapted copy against the uninstrumented fixture on loopback."""
import argparse
import contextlib
import select
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent
TESTS = HERE.parents[1] / "tests"
sys.path.insert(0, str(TESTS))
from test_capture import capturectl, cli, config, mock_process, pcap, request, wait_worker_count


@contextlib.contextmanager
def proxy(path, *args):
    with subprocess.Popen([sys.executable, str(path), *map(str, args)],
                          stdout=subprocess.PIPE, text=True) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            if not ready:
                raise SystemExit(f"proxy startup timed out: {path}")
            yield int(process.stdout.readline())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def responses(port):
    return [request(port, stream) for stream in (False, True)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=TESTS / "mock_proxy.py")
    args = parser.parse_args()
    original = HERE / "fixtures/proxy.py"
    with TemporaryDirectory() as work, mock_process("--backend") as (_, backend_port):
        cfg = Path(work) / "capture.json"
        config(cfg, compress=False)
        with proxy(original, "--backend-port", backend_port) as port:
            expected = responses(port)
        if any(status != 200 for status, _, _ in expected):
            raise SystemExit("fixture did not forward successful responses")
        with proxy(args.candidate, "--backend-port", backend_port, "--capture-config", cfg) as port:
            wait_worker_count(cfg, 1)
            if responses(port) != expected:
                raise SystemExit("candidate changed responses with capture off")
            enabled = cli("--config", cfg, "on", "--duration", 10)
            if enabled.returncode != 0:
                raise SystemExit(enabled.stderr or enabled.stdout)
            try:
                if responses(port) != expected:
                    raise SystemExit("candidate changed responses with capture on")
            finally:
                disabled = cli("--config", cfg, "off")
                if disabled.returncode != 0:
                    raise SystemExit(disabled.stderr or disabled.stdout)
            deadline = time.monotonic() + 10
            while capturectl.active_state(cfg)["writers"]:
                if time.monotonic() >= deadline:
                    raise SystemExit("writer did not drain")
                time.sleep(0.2)
            rows = list(pcap.iter_requests([str(cfg.parent / "data")]))
            if len(rows) != 2 or any(row.end is None for row in rows):
                raise SystemExit("expected two completed requests, both from the enabled window")


if __name__ == "__main__":
    main()
