"""Compare status evidence for a mock without capture and an idle worker."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests"))
from mock_support import cli, config, mock_process, wait_worker_count


def main():
    with TemporaryDirectory() as work:
        cfg = Path(work) / "capture.json"
        config(cfg)
        original = cfg.read_bytes()
        launch = json.loads((HERE / "fixtures/start.json").read_text())
        with mock_process(*launch["args"]) as (_, backend_port):
            absent = cli("--config", cfg, "status", "--json")
            if absent.returncode != 4 or json.loads(absent.stdout)["workers"]:
                raise SystemExit("status treated an inactive integration as an active worker")
            with mock_process("--backend-port", backend_port, "--capture-config", cfg):
                wait_worker_count(cfg, 1)
                idle = cli("--config", cfg, "status", "--json")
                workers = json.loads(idle.stdout)["workers"]
                if idle.returncode != 0 or len(workers) != 1 or workers[0]["enabled"]:
                    raise SystemExit("status did not report a connected, disabled worker")
        if cfg.read_bytes() != original or (cfg.parent / "data").exists():
            raise SystemExit("read-only status changed the configuration or collected data")


if __name__ == "__main__":
    main()
