"""Real Ray image-pipeline acceptance, including failure paths."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/image-smoke"


def invoke(name, argv, expected=0, timeout=240):
    process = subprocess.Popen(
        [sys.executable, *argv],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        (OUT / f"{name}.stdout").write_text(stdout, encoding="utf-8")
        (OUT / f"{name}.stderr").write_text(stderr, encoding="utf-8")
        raise ValueError(f"{name}: wrapper exceeded {timeout}s")
    (OUT / f"{name}.stdout").write_text(stdout, encoding="utf-8")
    (OUT / f"{name}.stderr").write_text(stderr, encoding="utf-8")
    if process.returncode != expected:
        raise ValueError(
            f"{name}: expected exit {expected}, got {process.returncode}; "
            "see stderr"
        )


def ray_processes():
    if not Path("/proc").is_dir():
        return []
    found = []
    markers = ("raylet", "plasma_store", "gcs_server", "ray::", "kp-ray-")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (
                entry.joinpath("cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except OSError:
            continue
        if any(marker in command for marker in markers):
            found.append({"pid": entry.name, "command": command.strip()})
    return found


def wait_for_ray_cleanup(timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = ray_processes()
        if not remaining:
            return []
        time.sleep(0.2)
    return ray_processes()


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    result = {"success": False, "verified_rows": 0}
    try:
        invoke("environment", ["scripts/check_environment.py"])
        invoke(
            "fixture",
            [
                "scripts/make_fixture.py",
                "--output",
                str(OUT / "fixture"),
                "--count",
                "16",
            ],
        )
        invoke(
            "fixture-repeat",
            [
                "scripts/make_fixture.py",
                "--output",
                str(OUT / "fixture-repeat"),
                "--count",
                "16",
            ],
        )
        first = [
            path.read_bytes()
            for path in sorted((OUT / "fixture").glob("*.jpg"))
        ]
        second = [
            path.read_bytes()
            for path in sorted((OUT / "fixture-repeat").glob("*.jpg"))
        ]
        if first != second:
            raise ValueError("synthetic JPEG fixture is not deterministic")
        fixture = OUT / "fixture"
        pipeline = [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(OUT / "pipeline"),
            "--timeout",
            "180",
        ]
        invoke("pipeline", pipeline)
        value = json.loads((OUT / "pipeline/result.json").read_text())
        if not value["correctness"] or value["output_rows"] != 16:
            raise ValueError("pipeline correctness/count failed")
        if value["inference"] != "mock" or value["gpu"] is not False:
            raise ValueError("pipeline did not record mock CPU inference")
        before = hashlib.sha256(
            (OUT / "pipeline/result.json").read_bytes()
        ).hexdigest()
        invoke("no-overwrite", pipeline, expected=1)
        if (
            hashlib.sha256(
                (OUT / "pipeline/result.json").read_bytes()
            ).hexdigest()
            != before
        ):
            raise ValueError("existing evidence was changed")
        user = OUT / "user-layout"
        for name in ("class-a", "class-b"):
            (user / name).mkdir(parents=True)
        for index, path in enumerate(sorted(fixture.glob("*.jpg"))[:6]):
            suffix = ".JPEG" if index == 0 else ".jpg"
            target = (
                user
                / ("class-a" if index % 2 == 0 else "class-b")
                / f"image-{index:02d}{suffix}"
            )
            shutil.copyfile(path, target)
        (user / "README.txt").write_text("not a JPEG", encoding="utf-8")
        invoke(
            "user-layout",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(user),
                "--output",
                str(OUT / "user-result"),
                "--limit",
                "4",
                "--batch-size",
                "2",
                "--cpus",
                "2",
            ],
        )
        user_value = json.loads((OUT / "user-result/result.json").read_text())
        if (
            user_value["dataset"] != "user-provided-jpeg-directory"
            or user_value["selected_rows"] != 4
            or user_value["output_rows"] != 4
            or not user_value["correctness"]
        ):
            raise ValueError("user-layout selection/validation failed")
        bad = OUT / "bad-jpeg"
        bad.mkdir()
        (bad / "bad.jpg").write_bytes(b"not a JPEG")
        invoke(
            "bad-jpeg",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(bad),
                "--output",
                str(OUT / "bad-result"),
            ],
            expected=1,
        )
        if (
            json.loads((OUT / "bad-result/result.json").read_text())["status"]
            != "failed"
        ):
            raise ValueError("corrupt JPEG incorrectly passed")
        empty = OUT / "empty"
        empty.mkdir()
        invoke(
            "empty-input",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(empty),
                "--output",
                str(OUT / "empty-result"),
            ],
            expected=1,
        )
        text_only = OUT / "text-only"
        text_only.mkdir()
        (text_only / "notes.txt").write_text("not an image", encoding="utf-8")
        invoke(
            "missing-jpeg",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(text_only),
                "--output",
                str(OUT / "missing-jpeg-result"),
            ],
            expected=1,
        )
        invoke(
            "bad-limit",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(fixture),
                "--output",
                str(OUT / "bad-limit-result"),
                "--limit",
                "0",
            ],
            expected=2,
        )
        invoke(
            "bad-batch-size",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(fixture),
                "--output",
                str(OUT / "bad-batch-result"),
                "--batch-size",
                "0",
            ],
            expected=2,
        )
        invoke(
            "bad-cpus",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(fixture),
                "--output",
                str(OUT / "bad-cpus-result"),
                "--cpus",
                "1",
            ],
            expected=2,
        )
        invoke("bad-timeout", [*pipeline, "--timeout", "nan"], expected=2)
        invoke(
            "memory-preflight",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(fixture),
                "--output",
                str(OUT / "memory-result"),
                "--max-estimated-mib",
                "1",
            ],
            expected=1,
        )
        if (OUT / "memory-result").exists():
            raise ValueError("memory preflight created an output directory")
        invoke(
            "outer-timeout",
            [
                "scripts/run_pipeline.py",
                "--input",
                str(fixture),
                "--output",
                str(OUT / "timeout-result"),
                "--timeout",
                "0.001",
            ],
            expected=124,
        )
        if (OUT / "timeout-result").exists():
            raise ValueError("outer timeout created an output directory")
        leftovers = wait_for_ray_cleanup()
        if leftovers:
            raise ValueError(f"Ray processes remain after tests: {leftovers}")
        result.update(
            success=True,
            verified_rows=16,
            fixture_deterministic=True,
            user_layout_verified=True,
            mock_inference_explicit=True,
            corrupt_jpeg_rejected=True,
            existing_evidence_preserved=True,
            missing_or_empty_input_rejected=True,
            invalid_arguments_rejected=True,
            invalid_timeout_rejected=True,
            memory_preflight_rejected=True,
            outer_timeout_returned_124=True,
            cleanup_no_ray_processes=True,
        )
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        (OUT / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
