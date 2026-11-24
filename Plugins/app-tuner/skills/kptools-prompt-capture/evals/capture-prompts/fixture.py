"""Serve or verify a timed metadata-only collection with a loopback proxy."""
import argparse
import contextlib
import json
import signal
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests"))
from mock_support import cli, config, mock_process, request, wait_worker_count
import pcap


@contextlib.contextmanager
def instance(work):
    cfg = work / "capture.json"
    config(cfg, compress=False)
    with mock_process("--backend") as (_, backend_port):
        with mock_process("--backend-port", backend_port, "--capture-config", cfg) as (_, port):
            wait_worker_count(cfg, 1)
            yield cfg, port


def status(cfg):
    result = cli("--config", cfg, "status", "--json")
    result.check_returncode()
    return json.loads(result.stdout)


def verify():
    with TemporaryDirectory() as temporary, instance(Path(temporary)) as (cfg, port):
        cli("--config", cfg, "on", "--duration", 4, "--metadata-only").check_returncode()
        if not status(cfg)["workers"][0]["enabled"]:
            raise SystemExit("capture did not become effective")
        if cli("--config", cfg, "set", "text_rate=1").returncode != 2:
            raise SystemExit("an active metadata-only worker accepted a text setting")
        if request(port, session="inside-window")[0] != 200:
            raise SystemExit("mock rejected request inside the capture window")
        deadline = time.monotonic() + 8
        while status(cfg)["workers"][0]["enabled"]:
            if time.monotonic() >= deadline:
                raise SystemExit("worker did not stop accepting records by the deadline")
            time.sleep(0.2)
        if request(port, session="after-window")[0] != 200:
            raise SystemExit("mock rejected request after the capture window")
        cli("--config", cfg, "off").check_returncode()
        deadline = time.monotonic() + 8
        while status(cfg)["writers"]:
            if time.monotonic() >= deadline:
                raise SystemExit("writer did not drain")
            time.sleep(0.2)
        rows = list(pcap.iter_requests([str(cfg.parent / "data")]))
        observed = [(row.rec["sess"], row.text_level, row.end is not None) for row in rows]
        if observed != [("inside-window", False, True)]:
            raise SystemExit(f"expected one completed metadata record inside the window: {observed}")
        if any("PRIVATE_SENTINEL" in part.read_text()
               for part in (cfg.parent / "data").rglob("seg-*.jsonl")):
            raise SystemExit("metadata collection stored request text")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", type=Path, help="launch a test fixture in a new directory")
    mode.add_argument("--request", type=int, help="send one synthetic request to the fixture port")
    args = parser.parse_args()
    if args.serve is not None:
        args.serve.mkdir(mode=0o700)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        with instance(args.serve.resolve()) as (cfg, port):
            print(json.dumps({"port": port, "config": str(cfg), "data": str(cfg.parent / "data")}), flush=True)
            stop.wait(120)
    elif args.request is not None:
        if request(args.request)[0] != 200:
            raise SystemExit("mock rejected synthetic request")
    else:
        verify()


if __name__ == "__main__":
    main()
