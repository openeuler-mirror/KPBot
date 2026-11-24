#!/usr/bin/env python3
"""Run a bounded local Ray Data microbenchmark in one owned process session."""

import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time

EXPECTED = {"ray": "2.56.0", "numpy": "2.1.3", "pyarrow": "23.0.1"}
OBJECT_STORE_BYTES = 256 * 1024 * 1024
SEED = 20261124


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def terminate_owned_group(process):
    """Signal only the new session created by this parent."""
    if process is None:
        return
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return


def preflight(config, output):
    problems = []
    evidence = {
        "expected_versions": EXPECTED,
        "actual_versions": {},
        "object_store_memory_bytes": OBJECT_STORE_BYTES,
        "ray_init_called": False,
    }
    if platform.system() != "Linux":
        problems.append("Linux is required")
    if sys.version_info[:2] != (3, 11):
        problems.append("Python 3.11 is required")
    for package, expected in EXPECTED.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        evidence["actual_versions"][package] = actual
        if actual != expected:
            problems.append(f"{package} must be {expected}; actual={actual}")
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count()
    evidence["visible_cpus"] = affinity
    if affinity is None or affinity < 2:
        problems.append("at least two visible CPUs are required")
    try:
        usage = shutil.disk_usage("/dev/shm")
        evidence["shm"] = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }
        if min(usage.total, usage.free) < OBJECT_STORE_BYTES:
            problems.append("free /dev/shm is smaller than the object store")
    except OSError as error:
        problems.append("cannot inspect /dev/shm: " + str(error))
    available = []
    try:
        for line in (
            Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
        ):
            if line.startswith("MemAvailable:"):
                evidence["mem_available_bytes"] = int(line.split()[1]) * 1024
                available.append(evidence["mem_available_bytes"])
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        cgroup = Path("/sys/fs/cgroup")
        maximum = (cgroup / "memory.max").read_text(encoding="ascii").strip()
        current = int(
            (cgroup / "memory.current").read_text(encoding="ascii").strip()
        )
        if maximum != "max":
            evidence["cgroup_remaining_bytes"] = max(0, int(maximum) - current)
            available.append(evidence["cgroup_remaining_bytes"])
    except (OSError, ValueError):
        pass
    if not available:
        problems.append("finite available memory cannot be established")
    elif min(available) < OBJECT_STORE_BYTES:
        problems.append("available memory is smaller than the object store")
    evidence["available_memory_upper_bound_bytes"] = (
        min(available) if available else None
    )
    try:
        disk = shutil.disk_usage(output)
        evidence["output_free_bytes"] = disk.free
        if disk.free < 16 * 1024 * 1024 or not os.access(
            output, os.W_OK | os.X_OK
        ):
            problems.append(
                "output directory lacks writability or 16 MiB free space"
            )
    except OSError as error:
        problems.append("cannot inspect output directory: " + str(error))
    if problems:
        return problems, evidence
    try:
        np = importlib.import_module("numpy")
        pa = importlib.import_module("pyarrow")
        values = np.arange(8, dtype=np.int64) * 2 + 1
        actual = (
            pa.Table.from_arrays([pa.array(values)], names=["value"])
            .column(0)
            .to_pylist()
        )
        if actual != list(range(1, 16, 2)):
            problems.append(
                "NumPy/PyArrow native smoke returned incorrect values"
            )
        ray = importlib.import_module("ray")
        data = importlib.import_module("ray.data")
        required = {
            "ray.init": (
                ray.init,
                (
                    "num_cpus",
                    "include_dashboard",
                    "object_store_memory",
                    "_temp_dir",
                ),
            ),
            "ray.shutdown": (ray.shutdown, ()),
            "ray.data.range": (data.range, ("override_num_blocks",)),
            "ray.data.Dataset.map_batches": (
                data.Dataset.map_batches,
                ("fn", "batch_size", "batch_format"),
            ),
            "ray.data.Dataset.materialize": (data.Dataset.materialize, ()),
            "ray.data.Dataset.take_all": (data.Dataset.take_all, ()),
        }
        evidence["api_checked"] = []
        for name, (function, parameters) in required.items():
            signature = inspect.signature(function)
            accepts_kwargs = any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in signature.parameters.values()
            )
            missing = (
                []
                if accepts_kwargs
                else [
                    item
                    for item in parameters
                    if item not in signature.parameters
                ]
            )
            if not callable(function) or missing:
                problems.append(f"incompatible API {name}; missing={missing}")
            evidence["api_checked"].append(name)
    except Exception as error:
        problems.append(
            f"native/API smoke failed: {type(error).__name__}: {error}"
        )
    return problems, evidence


def transform(batch, marker):
    """Deterministic NumPy UDF; one byte records each actual invocation."""
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, b"x")
    finally:
        os.close(descriptor)
    return {"id": batch["id"], "value": batch["id"] * 2 + 1}


def invocation_count(path):
    return path.stat().st_size if path.exists() else 0


def run_phase(ray, config, phase, output):
    rows = config["rows"]
    marker = output / (phase + ".executions")
    dataset = ray.data.range(rows, override_num_blocks=config["parallelism"])
    dataset = dataset.map_batches(
        transform,
        fn_kwargs={"marker": str(marker)},
        batch_size=config["batch_size"],
        batch_format="numpy",
    )
    started = time.perf_counter()
    materialized = dataset.materialize()
    elapsed = time.perf_counter() - started
    at_materialize = invocation_count(marker)
    records = materialized.take_all()
    after_read = invocation_count(marker)
    if after_read != at_materialize:
        raise RuntimeError("validation re-executed the measured pipeline")
    actual = {record["id"]: record["value"] for record in records}
    expected = {index: index * 2 + 1 for index in range(rows)}
    if len(records) != rows or actual != expected:
        raise RuntimeError("exact row IDs or values do not match the workload")
    return {
        "phase": phase,
        "measured": phase.startswith("measured-"),
        "wall_seconds": elapsed,
        "rows": len(records),
        "rows_per_second": rows / elapsed,
        "id_sum": sum(actual),
        "value_sum": sum(actual.values()),
        "expected_id_sum": rows * (rows - 1) // 2,
        "expected_value_sum": rows * rows,
        "exact_ids_values_count_sum": True,
        "udf_calls_at_materialize": at_materialize,
        "udf_calls_after_validation": after_read,
        "second_execution": False,
        "materialize_calls": 1,
    }


def worker(config):
    output = Path(config["output_dir"])
    result = {
        "schema_version": 1,
        "success": False,
        "started_at_utc": utc_now(),
        "status": "failed",
        "config": {
            key: value for key, value in config.items() if key != "output_dir"
        },
        "versions": {"python": platform.python_version(), **EXPECTED},
        "seed": SEED,
        "workload": "ray.data.range -> NumPy map_batches(value=id*2+1)",
        "timing_scope": (
            "perf_counter around exactly one Dataset.materialize per phase"
        ),
        "scheduling_scope": (
            "new local runtime; dataset construction, initialization and "
            "validation excluded from phase timing"
        ),
        "claims_boundary": (
            "microbenchmark observations only; no capacity, "
            "production-health or speedup claim"
        ),
        "phases": [],
    }
    ray = None
    temporary = None
    try:
        problems, evidence = preflight(config, output)
        result["preflight"] = evidence
        if problems:
            result["status"] = "blocked"
            result["error"] = "; ".join(problems)
            return 2
        import ray as ray_module

        ray = ray_module
        # Keep AF_UNIX socket paths short; result evidence remains in output.
        temporary = tempfile.TemporaryDirectory(prefix="kp-ray-bench-")
        init_started = time.perf_counter()
        ray.init(
            address="local",
            num_cpus=2,
            include_dashboard=False,
            object_store_memory=OBJECT_STORE_BYTES,
            _temp_dir=temporary.name,
            ignore_reinit_error=False,
        )
        result["initialization_wall_seconds"] = (
            time.perf_counter() - init_started
        )
        result["runtime"] = {
            "address": "local",
            "num_cpus": 2,
            "object_store_memory_bytes": OBJECT_STORE_BYTES,
            "owned_by_this_worker": True,
        }
        for index in range(config["warmup_rounds"]):
            result["phases"].append(
                run_phase(ray, config, f"warmup-{index + 1}", output)
            )
        for index in range(config["rounds"]):
            result["phases"].append(
                run_phase(ray, config, f"measured-{index + 1}", output)
            )
        measured = [phase for phase in result["phases"] if phase["measured"]]
        result["summary"] = {
            "measured_rounds": len(measured),
            "rows_per_round": config["rows"],
            "total_measured_wall_seconds": sum(
                item["wall_seconds"] for item in measured
            ),
            "mean_rows_per_second": sum(
                item["rows_per_second"] for item in measured
            )
            / len(measured),
            "all_exact": all(
                item["exact_ids_values_count_sum"]
                and not item["second_execution"]
                for item in measured
            ),
            "warmup_excluded": True,
        }
        result["success"] = True
        result["status"] = "completed"
        return 0
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return 1
    finally:
        if ray is not None:
            try:
                ray.shutdown()
                result["own_runtime_shutdown"] = True
            except Exception as error:
                result["success"] = False
                result["status"] = "failed"
                result["shutdown_error"] = f"{type(error).__name__}: {error}"
        if temporary is not None:
            temporary.cleanup()
        result["finished_at_utc"] = utc_now()
        (output / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )


def bounded_int(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be in {minimum}..{maximum}"
            )
        return number

    return parse


def timeout_value(value):
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= 3600:
        raise argparse.ArgumentTypeError(
            "timeout must be finite and in 1..3600"
        )
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new result directory"
    )
    parser.add_argument("--rows", type=bounded_int(1, 1000000), default=256)
    parser.add_argument("--batch-size", type=bounded_int(1, 65536), default=32)
    parser.add_argument("--parallelism", type=bounded_int(1, 64), default=4)
    parser.add_argument("--rounds", type=bounded_int(1, 10), default=3)
    parser.add_argument("--warmup-rounds", type=bounded_int(0, 3), default=1)
    parser.add_argument("--timeout", type=timeout_value, default=120.0)
    parser.add_argument("--worker-config", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.batch_size > args.rows:
        parser.error("--batch-size must not exceed --rows")
    return args


def parent(args):
    output = args.output_dir
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir(mode=0o700, exist_ok=False)
    except OSError as error:
        print(
            "cannot create fresh output directory: " + str(error),
            file=sys.stderr,
        )
        return 2
    config = {
        key: getattr(args, key)
        for key in (
            "rows",
            "batch_size",
            "parallelism",
            "rounds",
            "warmup_rounds",
        )
    }
    config.update(
        output_dir=str(output.resolve()), timeout_seconds=args.timeout
    )
    config_path = output / "config.json"
    config_path.write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--output-dir",
        str(output),
        "--worker-config",
        str(config_path),
    ]
    process = None
    control = {
        "worker_session_owned": True,
        "worker_pid": None,
        "timeout_seconds": args.timeout,
        "outcome": "not_started",
    }
    try:
        with (output / "benchmark.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            control["worker_pid"] = process.pid
            try:
                returncode = process.wait(timeout=args.timeout)
                control["outcome"] = "worker_exited"
            except subprocess.TimeoutExpired:
                control["outcome"] = "hard_wall_timeout"
                terminate_owned_group(process)
                returncode = 124
        if returncode != 0 and not (output / "result.json").exists():
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "success": False,
                        "status": "failed",
                        "error": control["outcome"],
                        "returncode": returncode,
                        "phases": [],
                        "finished_at_utc": utc_now(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return returncode
    except BaseException as error:
        control["outcome"] = "interrupted_" + type(error).__name__
        terminate_owned_group(process)
        if not (output / "result.json").exists():
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "success": False,
                        "status": "interrupted",
                        "error": str(error),
                        "phases": [],
                        "finished_at_utc": utc_now(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        terminate_owned_group(process)
        (output / "runtime_control.json").write_text(
            json.dumps(control, indent=2) + "\n", encoding="utf-8"
        )


def main(argv=None):
    args = parse_args(argv)
    if args.worker_config:
        config = json.loads(
            Path(args.worker_config).read_text(encoding="utf-8")
        )
        signal.signal(
            signal.SIGTERM,
            lambda _sig, _frame: (_ for _ in ()).throw(
                TimeoutError("SIGTERM")
            ),
        )
        return worker(config)
    return parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
