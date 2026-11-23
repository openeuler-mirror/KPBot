"""Run a bounded Ray JPEG/preprocessing pipeline with mock batch inference."""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_environment import inspect

_TRANSFORM = None
_TENSOR_BYTES = 3 * 224 * 224 * 4


def jpeg_paths(directory, limit):
    if not directory.is_dir():
        raise ValueError("input must be an existing JPEG directory")
    paths = sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg")
    )
    if not paths:
        raise ValueError("no JPEG files found")
    return paths[:limit]


def validate_args(args):
    if not 1 <= args.limit <= 10000:
        raise ValueError("limit must be 1..10000")
    if not 1 <= args.batch_size <= 256:
        raise ValueError("batch-size must be 1..256")
    if not 2 <= args.cpus <= 32:
        raise ValueError("cpus must be 2..32")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not 1 <= args.max_estimated_mib <= 65536:
        raise ValueError("max-estimated-mib must be 1..65536")


def estimate_memory(paths, max_estimated_mib):
    input_bytes = sum(path.stat().st_size for path in paths)
    # Driver reference tensors, Ray object-store blocks and worker batches.
    estimated_mib = (
        input_bytes + 3 * len(paths) * _TENSOR_BYTES + 64 * 1024 * 1024
    ) / (1024 * 1024)
    if estimated_mib > max_estimated_mib:
        raise ValueError(
            f"estimated peak {estimated_mib:.0f} MiB exceeds "
            f"--max-estimated-mib {max_estimated_mib}; reduce --limit"
        )
    return input_bytes, estimated_mib


def preprocess(row):
    global _TRANSFORM
    import numpy as np
    import torch
    from PIL import Image
    from torchvision.models import ResNet18_Weights

    torch.set_num_threads(1)
    if _TRANSFORM is None:
        # Weight metadata only; this never downloads model parameters.
        _TRANSFORM = ResNet18_Weights.DEFAULT.transforms()
    with Image.open(io.BytesIO(row["jpeg"])) as image:
        tensor = _TRANSFORM(image.convert("RGB"))
    value = tensor.numpy()
    if value.shape != (3, 224, 224) or not np.isfinite(value).all():
        raise ValueError("invalid preprocessed tensor")
    return {
        "id": row["id"],
        "tensor": value,
        "tensor_sha256": hashlib.sha256(value.tobytes()).hexdigest(),
    }


class MockActor:
    def __call__(self, batch):
        import numpy as np

        size = len(batch["id"])
        if batch["tensor"].shape != (size, 3, 224, 224):
            raise ValueError("unexpected batch tensor shape")
        return {
            "id": batch["id"],
            "tensor_sha256": batch["tensor_sha256"],
            "label": np.full(size, "mock_label"),
        }


def execute(args):
    validate_args(args)
    paths = jpeg_paths(args.input, args.limit)
    input_bytes, estimated_mib = estimate_memory(paths, args.max_estimated_mib)
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        "status": "failed",
        "inference": "mock",
        "preprocessing": "torchvision-ResNet18_Weights.DEFAULT.transforms",
        "model": "resnet18-weights-metadata-only-no-parameter-download",
        "gpu": False,
        "dataset": "synthetic"
        if (args.input / "fixture.json").is_file()
        else "user-provided-jpeg-directory",
        "selected_rows": len(paths),
        "batch_size": args.batch_size,
        "ray_logical_cpus": args.cpus,
        "input_bytes": input_bytes,
        "estimated_peak_mib": round(estimated_mib, 3),
        "max_estimated_mib": args.max_estimated_mib,
        "correctness": False,
    }
    ray = None
    session = tempfile.TemporaryDirectory(prefix="kp-ray-")
    try:
        result["environment"] = inspect()
        records, expected, manifest = [], {}, []
        # Bounded driver input for installation acceptance, not a full loader.
        for path in paths:
            data = path.read_bytes()
            name = path.relative_to(args.input).as_posix()
            row = {"id": name, "jpeg": data}
            expected[name] = preprocess(row)["tensor_sha256"]
            records.append(row)
            manifest.append(
                {
                    "id": name,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        (args.output / "input-manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        import ray
        from ray.data import ActorPoolStrategy

        ray.init(
            num_cpus=args.cpus,
            include_dashboard=False,
            object_store_memory=256 * 1024 * 1024,
            _temp_dir=session.name,
        )
        start = time.perf_counter()
        ds = (
            ray.data.from_items(records, override_num_blocks=2)
            .map(preprocess, num_cpus=1)
            .map_batches(
                MockActor,
                batch_size=args.batch_size,
                batch_format="numpy",
                compute=ActorPoolStrategy(size=1),
                num_cpus=1,
            )
        )
        # Execute once; reading stats must not run the Dataset again.
        rows = ds.take_all()
        result["pipeline_elapsed_seconds"] = time.perf_counter() - start
        result["output_rows"] = len(rows)
        actual = {str(row["id"]): str(row["tensor_sha256"]) for row in rows}
        if len(rows) != len(expected) or actual != expected:
            raise ValueError(
                "Ray output differs from serial preprocessing reference"
            )
        if any(str(row["label"]) != "mock_label" for row in rows):
            raise ValueError("mock inference labels differ")
        stats = ds.stats()
        if not stats.strip():
            raise ValueError("Ray returned empty Dataset stats")
        (args.output / "dataset-stats.txt").write_text(stats, encoding="utf-8")
        (args.output / "rows.json").write_text(
            json.dumps(
                [
                    {
                        "id": str(row["id"]),
                        "tensor_sha256": str(row["tensor_sha256"]),
                        "label": str(row["label"]),
                    }
                    for row in rows
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        result.update(status="passed", correctness=True)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if ray is not None:
            ray.shutdown()
        session.cleanup()
        (args.output / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-estimated-mib", type=int, default=4096)
    args = parser.parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    if os.environ.get("KPBOT_RAY_PIPELINE_CHILD") != "1":
        env = dict(os.environ, KPBOT_RAY_PIPELINE_CHILD="1")
        child = subprocess.Popen(
            [sys.executable, __file__, *sys.argv[1:]],
            env=env,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            print(
                "pipeline timed out/interrupted; "
                "owned process group terminated",
                file=sys.stderr,
            )
            code = 124
        raise SystemExit(code)
    try:
        result = execute(args)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"pipeline failed: {exc}\n")
    print(json.dumps(result))
    if result["status"] != "passed":
        print(result.get("error", "pipeline failed"), file=sys.stderr)
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
