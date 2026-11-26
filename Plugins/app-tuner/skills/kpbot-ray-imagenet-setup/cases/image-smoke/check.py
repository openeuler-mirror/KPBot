"""Real Ray image-pipeline acceptance, including failure paths."""

import argparse
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


def default_output():
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = ROOT / "results" / f"image-smoke-{stamp}-{os.getpid()}"
    candidate, suffix = base, 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    return candidate


def invoke(out, name, argv, expected=0, timeout=240, require_stderr=None):
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
        (out / f"{name}.stdout").write_text(stdout, encoding="utf-8")
        (out / f"{name}.stderr").write_text(stderr, encoding="utf-8")
        raise ValueError(f"{name}: wrapper exceeded {timeout}s")
    (out / f"{name}.stdout").write_text(stdout, encoding="utf-8")
    (out / f"{name}.stderr").write_text(stderr, encoding="utf-8")
    if process.returncode != expected:
        raise ValueError(
            f"{name}: expected exit {expected}, got {process.returncode}; "
            "see stderr"
        )
    if require_stderr and require_stderr not in stderr:
        raise ValueError(
            f"{name}: stderr does not mention {require_stderr!r}: {stderr}"
        )
    return stdout, stderr


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


def actor_swap_check():
    import numpy as np

    sys.path.insert(0, str(ROOT / "scripts"))
    from run_pipeline import MockActor

    first = np.zeros((3, 224, 224), dtype=np.float32)
    second = np.ones((3, 224, 224), dtype=np.float32)

    def digest(value):
        return hashlib.sha256(value.tobytes()).hexdigest()

    upstream = np.array([digest(first), digest(second)])
    received = MockActor()(
        {
            "id": np.array(["first", "second"]),
            "tensor": np.stack([second, first]),
            "tensor_sha256": upstream,
        }
    )
    actual = [digest(second), digest(first)]
    if [str(value) for value in received["tensor_sha256"]] != actual:
        raise ValueError("MockActor did not hash the tensors it received")
    serial = {"first": digest(first), "second": digest(second)}
    observed = {
        str(row_id): str(value)
        for row_id, value in zip(received["id"], received["tensor_sha256"])
    }
    if observed == serial:
        raise ValueError("same-shape swapped tensor was not detected")
    try:
        MockActor()(
            {
                "id": np.array(["first", "second"]),
                "tensor": np.stack([first, second]).astype(np.float64),
                "tensor_sha256": upstream,
            }
        )
    except ValueError as exc:
        if "dtype" not in str(exc):
            raise ValueError(f"unexpected dtype rejection: {exc}") from exc
    else:
        raise ValueError("MockActor accepted an unexpected tensor dtype")
    return True


def large_jpeg(path):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8000, 8000), (11, 22, 33)).save(path, quality=95)


def run(out):
    result = {
        "success": False,
        "verified_rows": 0,
        "output_dir": str(out),
        "result_json": str(out / "result.json"),
    }
    invoke(out, "environment", ["scripts/check_environment.py"])
    invoke(
        out,
        "fixture",
        [
            "scripts/make_fixture.py",
            "--output",
            str(out / "fixture"),
            "--count",
            "16",
        ],
    )
    invoke(
        out,
        "fixture-repeat",
        [
            "scripts/make_fixture.py",
            "--output",
            str(out / "fixture-repeat"),
            "--count",
            "16",
        ],
    )
    first = [
        path.read_bytes() for path in sorted((out / "fixture").glob("*.jpg"))
    ]
    second = [
        path.read_bytes()
        for path in sorted((out / "fixture-repeat").glob("*.jpg"))
    ]
    if first != second:
        raise ValueError("synthetic JPEG fixture is not deterministic")
    fixture = out / "fixture"
    pipeline = [
        "scripts/run_pipeline.py",
        "--input",
        str(fixture),
        "--output",
        str(out / "pipeline"),
        "--timeout",
        "180",
        "--dataset-kind",
        "synthetic",
    ]
    invoke(out, "pipeline", pipeline)
    value = json.loads((out / "pipeline/result.json").read_text())
    if not value["correctness"] or value["output_rows"] != 16:
        raise ValueError("pipeline correctness/count failed")
    if value["inference"] != "mock" or value["gpu"] is not False:
        raise ValueError("pipeline did not record mock CPU inference")
    if value["dataset"] != "synthetic":
        raise ValueError("fixture acceptance did not record synthetic data")
    if value["dataset_marker"]["kind"] != "synthetic-jpeg-smoke":
        raise ValueError("synthetic fixture marker was not recorded")
    before = hashlib.sha256(
        (out / "pipeline/result.json").read_bytes()
    ).hexdigest()
    invoke(
        out,
        "no-overwrite",
        pipeline,
        expected=2,
        require_stderr="output directory already exists",
    )
    if (
        hashlib.sha256((out / "pipeline/result.json").read_bytes()).hexdigest()
        != before
    ):
        raise ValueError("existing evidence was changed")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_exit_codes import check as check_exit_codes

    (out / "exit-codes.json").write_text(
        json.dumps(check_exit_codes(), indent=2), encoding="utf-8"
    )

    user = out / "user-layout"
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
    # A stray fixture.json must not relabel user data as synthetic.
    shutil.copyfile(fixture / "fixture.json", user / "fixture.json")
    invoke(
        out,
        "user-layout",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(user),
            "--output",
            str(out / "user-result"),
            "--limit",
            "4",
            "--batch-size",
            "2",
            "--cpus",
            "2",
        ],
    )
    user_value = json.loads((out / "user-result/result.json").read_text())
    if (
        user_value["dataset"] != "user-provided-jpeg-directory"
        or user_value["dataset_marker"] is not None
        or user_value["selected_rows"] != 4
        or user_value["output_rows"] != 4
        or not user_value["correctness"]
    ):
        raise ValueError("user-layout selection/validation failed")

    bad = out / "bad-jpeg"
    bad.mkdir()
    (bad / "bad.jpg").write_bytes(b"not a JPEG")
    invoke(
        out,
        "bad-jpeg",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(bad),
            "--output",
            str(out / "bad-result"),
        ],
        expected=1,
        require_stderr="bad.jpg",
    )
    if (out / "bad-result").exists():
        raise ValueError("corrupt JPEG created an output directory")

    renamed = out / "renamed-png"
    renamed.mkdir()
    from PIL import Image

    with Image.open(sorted(fixture.glob("*.jpg"))[0]) as image:
        image.save(renamed / "renamed.jpg", format="PNG")
    invoke(
        out,
        "renamed-png",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(renamed),
            "--output",
            str(out / "renamed-result"),
        ],
        expected=1,
        require_stderr="renamed.jpg",
    )
    if (out / "renamed-result").exists():
        raise ValueError("renamed PNG created an output directory")

    outside = out / "outside"
    outside.mkdir()
    shutil.copyfile(sorted(fixture.glob("*.jpg"))[0], outside / "real.jpg")
    linked = out / "symlink-input"
    linked.mkdir()
    (linked / "escape.jpg").symlink_to("../outside/real.jpg")
    invoke(
        out,
        "symlink-escape",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(linked),
            "--output",
            str(out / "symlink-result"),
        ],
        expected=1,
        require_stderr="escape.jpg",
    )
    if (out / "symlink-result").exists():
        raise ValueError("escaped symlink created an output directory")

    empty = out / "empty"
    empty.mkdir()
    invoke(
        out,
        "empty-input",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(empty),
            "--output",
            str(out / "empty-result"),
        ],
        expected=1,
    )
    text_only = out / "text-only"
    text_only.mkdir()
    (text_only / "notes.txt").write_text("not an image", encoding="utf-8")
    invoke(
        out,
        "missing-jpeg",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(text_only),
            "--output",
            str(out / "missing-jpeg-result"),
        ],
        expected=1,
    )
    invoke(
        out,
        "bad-limit",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "bad-limit-result"),
            "--limit",
            "0",
        ],
        expected=2,
    )
    invoke(
        out,
        "bad-batch-size",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "bad-batch-result"),
            "--batch-size",
            "0",
        ],
        expected=2,
    )
    invoke(
        out,
        "bad-cpus",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "bad-cpus-result"),
            "--cpus",
            "1",
        ],
        expected=2,
    )
    invoke(
        out,
        "bad-max-pixels",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "bad-pixels-result"),
            "--max-pixels",
            "0",
        ],
        expected=2,
    )
    invoke(out, "bad-timeout", [*pipeline, "--timeout", "nan"], expected=2)
    invoke(
        out,
        "memory-preflight",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "memory-result"),
            "--max-estimated-mib",
            "1",
        ],
        expected=1,
        require_stderr="estimated peak",
    )
    if (out / "memory-result").exists():
        raise ValueError("memory preflight created an output directory")

    large = out / "large-jpeg"
    large.mkdir()
    large_jpeg(large / "solid-8000x8000.jpg")
    invoke(
        out,
        "large-pixel-cap",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(large),
            "--output",
            str(out / "large-pixel-result"),
        ],
        expected=1,
        require_stderr="pixels",
    )
    if (out / "large-pixel-result").exists():
        raise ValueError("pixel cap created an output directory")
    invoke(
        out,
        "large-memory-preflight",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(large),
            "--output",
            str(out / "large-memory-result"),
            "--max-pixels",
            "200000000",
            "--max-estimated-mib",
            "100",
        ],
        expected=1,
        require_stderr="estimated peak",
    )
    if (out / "large-memory-result").exists():
        raise ValueError(
            "large-image memory preflight created an output directory"
        )

    if not actor_swap_check():
        raise ValueError("actor swap regression check failed")

    invoke(
        out,
        "outer-timeout",
        [
            "scripts/run_pipeline.py",
            "--input",
            str(fixture),
            "--output",
            str(out / "timeout-result"),
            "--timeout",
            "0.001",
        ],
        expected=124,
    )
    timeout_result = out / "timeout-result/result.json"
    if (
        timeout_result.exists()
        and json.loads(timeout_result.read_text())["status"] == "passed"
    ):
        raise ValueError("outer timeout reported success")

    leftovers = wait_for_ray_cleanup()
    if leftovers:
        raise ValueError(f"Ray processes remain after tests: {leftovers}")
    result.update(
        success=True,
        verified_rows=16,
        fixture_deterministic=True,
        user_layout_verified=True,
        source_kind_explicit=True,
        mock_inference_explicit=True,
        actor_hashes_received_tensor=True,
        swapped_tensor_detected=True,
        actor_dtype_rejected=True,
        corrupt_jpeg_rejected=True,
        renamed_png_rejected=True,
        symlink_escape_rejected=True,
        existing_evidence_preserved=True,
        existing_output_exit_code_2=True,
        worker_output_exit_code_2=True,
        missing_or_empty_input_rejected=True,
        invalid_arguments_rejected=True,
        invalid_timeout_rejected=True,
        memory_preflight_rejected=True,
        pixel_cap_enforced=True,
        outer_timeout_returned_124=True,
        cleanup_no_ray_processes=True,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new results directory; it must not already exist",
    )
    args = parser.parse_args()
    out = args.output_dir
    if out is None:
        out = default_output()
    elif not out.is_absolute():
        out = ROOT / out
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(
            f"output directory already exists: {out}; "
            "choose a new results directory instead of reusing stale evidence",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"cannot create output directory {out}: {exc}", file=sys.stderr)
        return 1

    result = {"success": False, "verified_rows": 0}
    try:
        result = run(out)
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        (out / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
