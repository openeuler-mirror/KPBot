"""Focused CLI regressions for output and failure exit-code contracts.

The reuse checks run before ``ray.init``, so these exercise the real parent
wrapper, real worker process and acceptance CLI without starting Ray.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "scripts" / "run_pipeline.py"
CHECK = ROOT / "cases" / "image-smoke" / "check.py"
DIAGNOSTIC = "output directory already exists"
CREATION_DIAGNOSTIC = "cannot create output directory"


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


def check_output_creation():
    with tempfile.TemporaryDirectory(prefix="kp-acceptance-exit-") as name:
        base = Path(name)
        existing = base / "existing"
        existing.mkdir()
        reuse = run_cli([str(CHECK), "--output-dir", str(existing)])
        expect(
            reuse.returncode == 2,
            f"acceptance existing output exit {reuse.returncode}, "
            f"expected 2: {reuse.stderr}",
        )
        expect(
            DIAGNOSTIC in reuse.stderr,
            f"acceptance stderr lacks {DIAGNOSTIC!r}: {reuse.stderr}",
        )

        spec = importlib.util.spec_from_file_location("image_smoke_check", CHECK)
        acceptance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance)
        output = base / "result"
        stderr = io.StringIO()
        with mock.patch.object(
            acceptance.Path,
            "mkdir",
            side_effect=OSError("injected output creation failure"),
        ):
            with mock.patch.object(
                sys,
                "argv",
                [str(CHECK), "--output-dir", str(output)],
            ), contextlib.redirect_stderr(stderr):
                failure_code = acceptance.main()
        failure_stderr = stderr.getvalue()
        expect(
            failure_code == 1,
            f"acceptance OSError exit {failure_code}, expected 1: "
            f"{failure_stderr}",
        )
        expect(
            CREATION_DIAGNOSTIC in failure_stderr,
            f"acceptance stderr lacks {CREATION_DIAGNOSTIC!r}: "
            f"{failure_stderr}",
        )
        expect(not output.exists(), "failed output creation left a result path")

    return {
        "existing_exit_code": reuse.returncode,
        "creation_failure_exit_code": failure_code,
    }


def check():
    output_creation = check_output_creation()
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
        "acceptance_existing_output_exit_code": output_creation[
            "existing_exit_code"
        ],
        "acceptance_output_creation_failure_exit_code": output_creation[
            "creation_failure_exit_code"
        ],
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
