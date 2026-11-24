#!/usr/bin/env python3
"""Run a bounded synthetic-JPEG Ray Data preprocessing smoke benchmark."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import time

import bench_support as support


SCHEMA_VERSION = 1
EXPECTED_VERSIONS = support.EXPECTED_VERSIONS
_TRANSFORM = None


class CorrectnessError(ValueError):
    """Ray output differs from the independent serial reference."""


def preprocess(row):
    """Decode JPEG bytes and apply the ResNet18 CPU preprocessing recipe."""
    global _TRANSFORM
    import numpy as np
    import torch
    from PIL import Image
    from torchvision.models import ResNet18_Weights

    torch.set_num_threads(1)
    if _TRANSFORM is None:
        # Reads weight metadata only; model parameters are never downloaded.
        _TRANSFORM = ResNet18_Weights.DEFAULT.transforms()
    started = time.perf_counter()
    with Image.open(io.BytesIO(row["jpeg"])) as image:
        if image.format != "JPEG":
            raise ValueError(f"input is not JPEG: {row['id']}")
        tensor = _TRANSFORM(image.convert("RGB"))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    value = tensor.numpy()
    if value.shape != (3, 224, 224) or value.dtype != np.float32:
        raise ValueError("invalid preprocessed tensor shape or dtype")
    if not np.isfinite(value).all():
        raise ValueError("preprocessed tensor contains non-finite values")
    return {
        "id": row["id"],
        "tensor": value,
        "tensor_sha256": hashlib.sha256(
            np.ascontiguousarray(value).tobytes()
        ).hexdigest(),
        "transform_latency_ms": elapsed_ms,
    }


class DigestActor:
    """Rehash tensors after Ray batching and measure actor batch latency."""

    def __call__(self, batch):
        import numpy as np

        started = time.perf_counter()
        tensors = batch["tensor"]
        size = len(batch["id"])
        if tensors.shape != (size, 3, 224, 224):
            raise ValueError("unexpected Ray batch tensor shape")
        if tensors.dtype != np.float32 or not np.isfinite(tensors).all():
            raise ValueError("unexpected Ray batch tensor dtype or values")
        digests = [
            hashlib.sha256(
                np.ascontiguousarray(tensors[index]).tobytes()
            ).hexdigest()
            for index in range(size)
        ]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "id": batch["id"],
            "tensor_sha256": np.array(digests),
            "transform_latency_ms": batch["transform_latency_ms"],
            "actor_batch_latency_ms": np.full(size, elapsed_ms),
        }


def verify_rows(expected, rows):
    """Require unique IDs and exact post-batching SHA-256 values."""
    if len(rows) != len(expected):
        raise CorrectnessError(
            f"row count differs: expected={len(expected)}, actual={len(rows)}"
        )
    actual = {}
    transform_latencies = []
    actor_latencies = []
    for row in rows:
        row_id = str(row["id"])
        if row_id in actual:
            raise CorrectnessError(f"duplicate output id: {row_id}")
        actual[row_id] = str(row["tensor_sha256"])
        transform = float(row["transform_latency_ms"])
        actor = float(row["actor_batch_latency_ms"])
        if not math.isfinite(transform) or transform < 0:
            raise CorrectnessError(f"invalid transform latency for {row_id}")
        if not math.isfinite(actor) or actor < 0:
            raise CorrectnessError(f"invalid actor latency for {row_id}")
        transform_latencies.append(transform)
        actor_latencies.append(actor)
    if actual != expected:
        differing = sorted(
            key
            for key in set(actual) | set(expected)
            if actual.get(key) != expected.get(key)
        )
        raise CorrectnessError(f"tensor digest mismatch for IDs: {differing}")
    return {
        "correct": True,
        "input_rows": len(expected),
        "output_rows": len(rows),
        "unique_output_ids": len(actual),
        "transform_latency_ms": {
            "min": min(transform_latencies),
            "median": statistics.median(transform_latencies),
            "max": max(transform_latencies),
        },
        "actor_batch_latency_ms_per_row": {
            "min": min(actor_latencies),
            "median": statistics.median(actor_latencies),
            "max": max(actor_latencies),
        },
    }


def generate_fixture(output, count):
    """Generate deterministic tiny RGB-pattern JPEGs, not ImageNet data."""
    from PIL import Image

    fixture = output / "fixture"
    fixture.mkdir(exist_ok=False)
    records = []
    manifest = []
    for index in range(count):
        width = 40 + index % 5
        height = 44 + index % 7
        pixels = bytes(
            (byte * 17 + index * 23) % 256
            for byte in range(width * height * 3)
        )
        buffer = io.BytesIO()
        Image.frombytes("RGB", (width, height), pixels).save(
            buffer, format="JPEG", quality=85
        )
        content = buffer.getvalue()
        row_id = f"sample-{index:03d}.jpg"
        support.write_bytes_exclusive(fixture / row_id, content)
        records.append({"id": row_id, "jpeg": content})
        manifest.append(
            {
                "id": row_id,
                "width": width,
                "height": height,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    document = {
        "kind": "deterministic-rgb-byte-jpeg-v1",
        "count": count,
        "is_imagenet": False,
        "files": manifest,
    }
    support.write_json_exclusive(fixture / "fixture.json", document)
    return records, document


def execute_round(ray, records, expected, args, kind, index):
    """Execute once and retain stats/raw rows on verification failure."""
    from ray.data import ActorPoolStrategy

    document = {
        "kind": kind,
        "index": index,
        "status": "failed",
        "correct": False,
    }
    started = time.perf_counter()
    try:
        dataset = (
            ray.data.from_items(records, override_num_blocks=2)
            .map(preprocess, num_cpus=1)
            .map_batches(
                DigestActor,
                batch_size=args.batch_size,
                batch_format="numpy",
                compute=ActorPoolStrategy(size=1),
                num_cpus=1,
            )
        )
        rows = dataset.take_all()
        document["wall_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
        stats = dataset.stats()
        stem = f"{kind}-{index:02d}"
        stats_name = f"{stem}-dataset-stats.txt"
        rows_name = f"{stem}-rows.json"
        support.write_text_exclusive(args.output / stats_name, stats)
        document["native_dataset_stats_file"] = stats_name
        document["native_dataset_stats"] = stats
        serializable_rows = [
            {
                "id": str(row["id"]),
                "tensor_sha256": str(row["tensor_sha256"]),
                "transform_latency_ms": float(row["transform_latency_ms"]),
                "actor_batch_latency_ms": float(row["actor_batch_latency_ms"]),
            }
            for row in rows
        ]
        support.write_json_exclusive(
            args.output / rows_name, serializable_rows
        )
        document["raw_rows_file"] = rows_name
        if not stats.strip():
            raise RuntimeError("Ray returned empty native Dataset stats")
        document.update(verify_rows(expected, rows))
        document["throughput_images_per_second"] = (
            len(rows) * 1000.0 / document["wall_elapsed_ms"]
        )
        document["status"] = "passed"
    except Exception as exc:
        document["wall_elapsed_ms"] = (time.perf_counter() - started) * 1000.0
        document["reason"] = (
            "correctness"
            if isinstance(exc, CorrectnessError)
            else support.classify_error(exc)
        )
        document["error"] = support.sanitize_text(
            f"{type(exc).__name__}: {exc}"
        )
    return document


def summarize_rounds(timed_rounds, warmup_rounds):
    """Summarize timed rounds only; warmup is never an input sample."""
    if len(timed_rounds) < 3 or any(
        item["status"] != "passed" or not item["correct"]
        for item in timed_rounds
    ):
        raise ValueError("at least three correct timed rounds are required")
    wall = [float(item["wall_elapsed_ms"]) for item in timed_rounds]
    throughput = [
        float(item["throughput_images_per_second"]) for item in timed_rounds
    ]
    transform_medians = [
        float(item["transform_latency_ms"]["median"]) for item in timed_rounds
    ]
    return {
        "scope": (
            "timed rounds only: from_items through take_all; excludes Ray "
            "initialization, fixture generation, serial reference and warmup"
        ),
        "timed_round_count": len(timed_rounds),
        "warmup_round_count_excluded": len(warmup_rounds),
        "warmup_excluded": True,
        "total_timed_images": sum(
            item["output_rows"] for item in timed_rounds
        ),
        "wall_elapsed_ms": {
            "samples": wall,
            "min": min(wall),
            "median": statistics.median(wall),
            "max": max(wall),
        },
        "throughput_images_per_second": {
            "samples": throughput,
            "min": min(throughput),
            "median": statistics.median(throughput),
            "max": max(throughput),
        },
        "median_of_per_round_transform_latency_ms": statistics.median(
            transform_medians
        ),
        "direction": {
            "wall_elapsed_ms": "lower_is_better",
            "throughput_images_per_second": "higher_is_better",
            "transform_latency_ms": "lower_is_better",
        },
    }


def execute_benchmark(args):
    """Execute the workload and return a fail-closed child result."""
    result = {
        "schema_version": SCHEMA_VERSION,
        "skill": "kptools-ray-imagenet-benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "success": False,
        "correctness": False,
        "scope": "synthetic-jpeg-ray-data-resnet18-preprocessing-smoke",
        "is_full_imagenet_benchmark": False,
        "full_imagenet_accuracy": None,
        "model_inference": False,
        "configuration": {
            "address": args.address,
            "owns_local_ray_runtime": args.address is None,
            "images": args.images,
            "batch_size": args.batch_size,
            "cpus": args.cpus,
            "object_store_mib": args.object_store_mib,
            "warmup_rounds": args.warmup,
            "timed_rounds": args.rounds,
            "timeout_seconds": args.timeout,
        },
        "warmup_rounds": [],
        "timed_rounds": [],
        "summary": None,
    }
    ray = None
    session = None
    try:
        prerequisites, versions = support.runtime_prerequisites(args)
        result["prerequisites"] = prerequisites
        result["versions"] = versions
        blocking = [
            item
            for item in prerequisites
            if item["mandatory"] and item["status"] != "ready"
        ]
        if blocking:
            result["failure_category"] = "prerequisite"
            result["error"] = "live prerequisites are not ready"
            return result
        records, fixture = generate_fixture(args.output, args.images)
        result["fixture"] = fixture
        result["input_bytes"] = sum(item["bytes"] for item in fixture["files"])
        expected = {
            row["id"]: preprocess(row)["tensor_sha256"] for row in records
        }
        support.write_json_exclusive(
            args.output / "serial-reference.json", expected
        )
        import ray as ray_module

        ray = ray_module
        if args.address:
            ray.init(
                address=args.address,
                include_dashboard=False,
                namespace=f"kptools-ray-imagenet-smoke-{os.getpid()}",
            )
        else:
            # Ray AF_UNIX socket names require a short system temporary path.
            session = tempfile.TemporaryDirectory(prefix="kptry-")
            ray.init(
                address="local",
                num_cpus=args.cpus,
                include_dashboard=False,
                object_store_memory=args.object_store_mib * 1024 * 1024,
                _temp_dir=session.name,
            )
        resources = dict(ray.cluster_resources())
        result["ray_cluster_resources"] = resources
        if float(resources.get("CPU", 0)) < args.cpus:
            raise RuntimeError("Ray cluster has fewer CPUs than requested")
        if args.address:

            @ray.remote
            def read_cluster_version():
                import ray as worker_ray

                return worker_ray.__version__

            cluster_version = str(
                ray.get(read_cluster_version.remote(), timeout=30)
            )
            result["versions"]["cluster_ray"] = cluster_version
            if (
                support.base_version(cluster_version)
                != EXPECTED_VERSIONS["ray"]
            ):
                raise ValueError(
                    f"existing cluster Ray {cluster_version} is unsupported"
                )
        for index in range(args.warmup):
            warmup = execute_round(
                ray, records, expected, args, "warmup", index
            )
            result["warmup_rounds"].append(warmup)
            if warmup["status"] != "passed":
                if warmup.get("reason") == "correctness":
                    raise CorrectnessError(warmup["error"])
                raise RuntimeError(f"warmup round {index} failed")
        for index in range(args.rounds):
            timed = execute_round(ray, records, expected, args, "timed", index)
            result["timed_rounds"].append(timed)
            if timed["status"] != "passed":
                if timed.get("reason") == "correctness":
                    raise CorrectnessError(timed["error"])
                raise RuntimeError(f"timed round {index} failed")
        result["summary"] = summarize_rounds(
            result["timed_rounds"], result["warmup_rounds"]
        )
        result["correctness"] = True
        result["status"] = "passed"
        result["success"] = True
    except Exception as exc:
        result["status"] = "failed"
        result["success"] = False
        result["correctness"] = False
        result["failure_category"] = (
            "correctness"
            if isinstance(exc, CorrectnessError)
            else support.classify_error(exc)
        )
        result["error"] = support.sanitize_text(f"{type(exc).__name__}: {exc}")
    finally:
        if ray is not None:
            try:
                # Also cleans up a partially initialized owned local runtime.
                # For an existing service this disconnects only this driver.
                ray.shutdown()
            except Exception as exc:
                result["cleanup_error"] = support.sanitize_text(
                    f"{type(exc).__name__}: {exc}"
                )
                if result["success"]:
                    result.update(
                        status="failed",
                        success=False,
                        correctness=False,
                        failure_category="cleanup",
                        error="Ray shutdown failed",
                    )
        if session is not None:
            try:
                session.cleanup()
            except OSError as exc:
                result["cleanup_error"] = support.sanitize_text(
                    f"{type(exc).__name__}: {exc}"
                )
                if result["success"]:
                    result.update(
                        status="failed",
                        success=False,
                        correctness=False,
                        failure_category="cleanup",
                        error="owned Ray session cleanup failed",
                    )
    return result


def redact_owned_file(path):
    """Redact declared credentials from a log created by this supervisor."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    redacted = support.sanitize_text(content)
    if redacted != content:
        path.write_text(redacted, encoding="utf-8")


def terminate_owned_group(process):
    """Terminate only the process group created by this supervisor."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit while a worker still ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait()


def validate_success_document(document, expected_rounds, expected_warmup):
    """Semantic parent-side validation; child claims alone are insufficient."""
    if (
        document.get("status") != "passed"
        or document.get("success") is not True
    ):
        return "child did not report a passing benchmark"
    if document.get("correctness") is not True:
        return "child did not prove exact correctness"
    timed = document.get("timed_rounds")
    warmup = document.get("warmup_rounds")
    if not isinstance(timed, list) or len(timed) != expected_rounds:
        return "timed round count does not match the requested contract"
    if not isinstance(warmup, list) or len(warmup) != expected_warmup:
        return "warmup round count does not match the requested contract"
    if any(
        item.get("status") != "passed" or item.get("correct") is not True
        for item in timed + warmup
    ):
        return "one or more rounds failed correctness or execution"
    summary = document.get("summary") or {}
    if summary.get("warmup_excluded") is not True:
        return "summary does not prove warmup exclusion"
    if summary.get("timed_round_count") != expected_rounds:
        return "summary timed round count is incorrect"
    return None


def supervise(command, output, timeout, validator=None, env=None):
    """Run one owned process group and always preserve a final result/logs."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)
    started = time.perf_counter()
    stdout_path = output / "stdout.log"
    stderr_path = output / "stderr.log"
    stdout_descriptor = os.open(
        stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    stderr_descriptor = os.open(
        stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    timed_out = False
    interrupted = False
    startup_error = None
    with os.fdopen(stdout_descriptor, "wb") as stdout_stream:
        with os.fdopen(stderr_descriptor, "wb") as stderr_stream:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    start_new_session=True,
                    env=env,
                )
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_owned_group(process)
                    returncode = process.returncode
                except KeyboardInterrupt:
                    interrupted = True
                    terminate_owned_group(process)
                    returncode = process.returncode
            except (FileNotFoundError, PermissionError, OSError) as exc:
                returncode = None
                startup_error = support.sanitize_text(
                    f"{type(exc).__name__}: {exc}"
                )
    redact_owned_file(stdout_path)
    redact_owned_file(stderr_path)
    child_path = output / "child-result.json"
    try:
        document = json.loads(child_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        document = None
    supervisor = {
        "returncode": returncode,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "wall_elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "stdout_log": stdout_path.name,
        "stderr_log": stderr_path.name,
        "process_group_policy": "owned-group-only",
    }
    if startup_error:
        supervisor["startup_error"] = startup_error
    if document is None:
        if timed_out:
            category = "timeout"
            status = "timeout"
            error = f"benchmark exceeded finite timeout {timeout} seconds"
        elif interrupted:
            category = "interrupted"
            status = "interrupted"
            error = "benchmark interrupted; owned process group terminated"
        else:
            category = "runtime_error"
            status = "failed"
            error = startup_error or "child exited without a valid result"
        document = {
            "schema_version": SCHEMA_VERSION,
            "skill": "kptools-ray-imagenet-benchmark",
            "status": status,
            "success": False,
            "correctness": False,
            "failure_category": category,
            "error": error,
            "warmup_rounds": [],
            "timed_rounds": [],
            "summary": None,
        }
    else:
        document = support.sanitize_value(document)
        if timed_out:
            document.update(
                status="timeout",
                success=False,
                correctness=False,
                failure_category="timeout",
                error=f"benchmark exceeded finite timeout {timeout} seconds",
            )
        elif interrupted:
            document.update(
                status="interrupted",
                success=False,
                correctness=False,
                failure_category="interrupted",
                error="benchmark interrupted; owned process group terminated",
            )
        elif returncode == 0:
            validation_error = validator(document) if validator else None
            if validation_error:
                document.update(
                    status="failed",
                    success=False,
                    correctness=False,
                    failure_category="contract",
                    error=validation_error,
                )
        else:
            document.update(success=False, correctness=False)
            if document.get("status") == "passed":
                document["status"] = "failed"
                document["failure_category"] = "runtime_error"
                document["error"] = (
                    "child returned success JSON with nonzero exit"
                )
    document["supervisor"] = supervisor
    support.write_json_exclusive(output / "result.json", document)
    if timed_out:
        return 124, document
    if interrupted:
        return 130, document
    if returncode == 0 and document.get("success") is True:
        return 0, document
    return 1, document


def finite_timeout(value):
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 900:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and in 1..900"
        )
    return number


def bounded_int(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be in {minimum}..{maximum}"
            )
        return number

    return parse


def validate_address(address):
    if address is None:
        return
    if (
        address == "local"
        or not address.strip()
        or any(char in address for char in ("@", "?", "#", "\n", "\r"))
    ):
        raise ValueError(
            "--address must name an existing Ray endpoint without userinfo, "
            "query or fragment; use secure Ray environment configuration"
        )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--address")
    parser.add_argument("--images", type=bounded_int(4, 64), default=8)
    parser.add_argument("--batch-size", type=bounded_int(1, 16), default=4)
    parser.add_argument("--cpus", type=bounded_int(2, 16), default=2)
    parser.add_argument(
        "--object-store-mib", type=bounded_int(64, 2048), default=256
    )
    parser.add_argument(
        "--min-memory-mib", type=bounded_int(1024, 65536), default=2048
    )
    parser.add_argument("--warmup", type=bounded_int(1, 5), default=1)
    parser.add_argument("--rounds", type=bounded_int(3, 20), default=3)
    parser.add_argument("--timeout", type=finite_timeout, default=240.0)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser


def child_main(args):
    try:
        result = execute_benchmark(args)
    except Exception as exc:  # Last-resort structured child failure.
        result = {
            "schema_version": SCHEMA_VERSION,
            "skill": "kptools-ray-imagenet-benchmark",
            "status": "failed",
            "success": False,
            "correctness": False,
            "failure_category": support.classify_error(exc),
            "error": support.sanitize_text(f"{type(exc).__name__}: {exc}"),
            "warmup_rounds": [],
            "timed_rounds": [],
            "summary": None,
        }
    support.write_json_exclusive(args.output / "child-result.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


def main(argv=None):
    if argv is None:
        argv = list(sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    args.output = args.output.expanduser().resolve()
    try:
        validate_address(args.address)
    except ValueError as exc:
        parser.error(str(exc))
    if args.child:
        if os.environ.get("KPBOT_RAY_BENCHMARK_CHILD") != "1":
            parser.error("--child is an internal supervisor interface")
        return child_main(args)
    if args.output.exists():
        print(f"output already exists: {args.output}", file=sys.stderr)
        return 2
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        *argv,
        "--child",
    ]
    environment = dict(os.environ)
    environment["KPBOT_RAY_BENCHMARK_CHILD"] = "1"

    def validator(document):
        return validate_success_document(document, args.rounds, args.warmup)

    try:
        code, result = supervise(
            command,
            args.output,
            args.timeout,
            validator=validator,
            env=environment,
        )
    except FileExistsError:
        print(f"output already exists: {args.output}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            support.sanitize_text(f"cannot create output: {exc}"),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
