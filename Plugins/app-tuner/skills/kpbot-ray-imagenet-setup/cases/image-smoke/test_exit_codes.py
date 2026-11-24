"""Focused CLI regression for the existing-output exit-code contract.

The reuse check runs before ``ray.init``, so this exercises the real parent
wrapper and the real worker process without starting Ray.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "scripts" / "run_pipeline.py"
DIAGNOSTIC = "output directory already exists"


def make_jpeg(path):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(path, format="JPEG")


def run_cli(argv, child_worker=False, timeout=120):
    env = dict(os.environ)
    env.pop("KPBOT_RAY_PIPELINE_CHILD", None)
    if child_worker:
        env["KPBOT_RAY_PIPELINE_CHILD"] = "1"
    return subprocess.run(
        [sys.executable, *argv],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def expect(condition, message):
    if not condition:
        raise ValueError(message)


def check():
    with tempfile.TemporaryDirectory(prefix="kp-exit-code-") as name:
        base = Path(name)
        source = base / "input"
        make_jpeg(source / "image.jpg")
        output = base / "output"
        output.mkdir()
        marker = output / "result.json"
        marker.write_text('{"status": "stale"}\n', encoding="utf-8")
        before = hashlib.sha256(marker.read_bytes()).hexdigest()
        argv = [
            str(PIPELINE),
            "--input",
            str(source),
            "--output",
            str(output),
        ]

        parent = run_cli(argv)
        expect(
            parent.returncode == 2,
            f"parent CLI exit {parent.returncode}, expected 2: "
            f"{parent.stderr}",
        )
        expect(
            DIAGNOSTIC in parent.stderr,
            f"parent CLI stderr lacks {DIAGNOSTIC!r}: {parent.stderr}",
        )
        worker = run_cli(argv, child_worker=True)
        expect(
            worker.returncode == 2,
            f"child worker exit {worker.returncode}, expected 2: "
            f"{worker.stderr}",
        )
        expect(
            DIAGNOSTIC in worker.stderr,
            f"child worker stderr lacks {DIAGNOSTIC!r}: {worker.stderr}",
        )
        after = hashlib.sha256(marker.read_bytes()).hexdigest()
        expect(after == before, "existing result bytes were changed")
        expect(
            sorted(path.name for path in output.iterdir()) == ["result.json"],
            "existing output directory gained new files",
        )

        empty = base / "empty"
        empty.mkdir()
        other_output = base / "other"
        other = run_cli(
            [
                str(PIPELINE),
                "--input",
                str(empty),
                "--output",
                str(other_output),
            ]
        )
        expect(
            other.returncode == 1,
            f"input failure exit {other.returncode}, expected 1: "
            f"{other.stderr}",
        )
        expect(
            not other_output.exists(),
            "input failure created an output directory",
        )

    return {
        "success": True,
        "parent_exit_code": parent.returncode,
        "worker_exit_code": worker.returncode,
        "diagnostic_present": True,
        "stale_result_preserved": True,
        "non_usage_failure_exit_code": other.returncode,
    }


def main():
    result = {"success": False}
    try:
        result = check()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
