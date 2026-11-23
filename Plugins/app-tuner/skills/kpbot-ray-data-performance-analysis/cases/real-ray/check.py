"""Collect real Ray evidence and compare six checked ETL runs."""

import importlib.metadata
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {"ray": "2.52.0", "numpy": "2.1.3", "pyarrow": "19.0.1"}
MIN_SHM_BYTES = 256 * 1024 * 1024


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / f"scripts/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight():
    """Report unmet tier-B prerequisites without importing Ray.

    The entrypoint (`run.sh`) owns the GNU-timeout check because it needs
    `timeout --kill-after` before Python starts; this function covers the
    runtime prerequisites that Python itself can inspect.
    """
    problems = []
    if not sys.platform.startswith("linux"):
        problems.append(f"platform {sys.platform} is not Linux")
    if sys.version_info[:2] != (3, 11):
        problems.append(
            f"Python {platform.python_version()} is not an isolated 3.11"
        )
    try:
        visible_cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        visible_cpus = os.cpu_count() or 1
    if visible_cpus < 2:
        problems.append(f"only {visible_cpus} visible CPU(s); need at least 2")
    for package, expected in REQUIRED.items():
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{package}=={expected} is not installed")
            continue
        if installed != expected:
            problems.append(f"{package} {installed} does not match {expected}")
    try:
        shm = shutil.disk_usage("/dev/shm")
    except OSError:
        problems.append("/dev/shm is unavailable")
    else:
        if shm.total < MIN_SHM_BYTES or shm.free < MIN_SHM_BYTES // 2:
            problems.append(
                "/dev/shm is too small: "
                f"total={shm.total} free={shm.free} bytes"
            )
    return problems


def mark(marker):
    """Append one byte so callers can count real UDF invocations."""
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, b"x")
    finally:
        os.close(descriptor)


def invocation_count(marker):
    return marker.stat().st_size if marker.exists() else 0


def row_udf(row, marker):
    mark(marker)
    return {"id": row["id"], "value": row["id"] ** 2}


def batch_udf(batch, marker):
    mark(marker)
    return {"id": batch["id"], "value": batch["id"] ** 2}


def main():
    output = ROOT / "results/real-ray"
    output.mkdir(parents=True, exist_ok=False)
    result = {"success": False, "verified_rounds": 0}
    problems = preflight()
    if problems:
        result.update(env_not_satisfied=True, reason="; ".join(problems))
        (output / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print("SKIP(env_not_satisfied): " + result["reason"], file=sys.stderr)
        print(json.dumps(result))
        return 3
    session = tempfile.TemporaryDirectory(prefix="kp-analysis-")
    marker_root = Path(session.name) / "executions"
    marker_root.mkdir()
    ray = None
    try:
        # Exercise the collector CLI using this runtime's resource snapshot.
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/inspect_environment.py"),
                "--out",
                str(output / "environment.json"),
            ],
            check=True,
        )
        import ray

        ray.init(
            num_cpus=2,
            include_dashboard=False,
            object_store_memory=256 * 1024 * 1024,
            _temp_dir=session.name,
        )
        context = {
            "workload": "generated-integer-etl",
            "input_id": "range-256-square-v1",
            "scope": "materialize-call",
            "resources": {
                "ray_logical_cpus": 2,
                "physical_limits_evidence": "environment.json",
            },
            "software": {
                "ray": ray.__version__,
                "python": platform.python_version(),
            },
            "cache_state": "new-dataset-each-round; no external storage",
            "correctness_contract": "256 unique ids and exact integer squares",
        }
        documents = {}
        for mode in ("row", "batch"):
            documents[mode] = {
                "schema_version": 1,
                "metric": {
                    "name": "elapsed",
                    "unit": "seconds",
                    "direction": "lower",
                },
                "context": {**context, "config": {"mode": mode}},
                "samples": [],
            }
        # Alternate modes; no claim of significance or benchmark benefit.
        for index in range(3):
            for mode in (
                ("row", "batch") if index % 2 == 0 else ("batch", "row")
            ):
                marker = marker_root / f"{mode}-{index}.exec"
                ds = ray.data.range(256, override_num_blocks=4)
                if mode == "row":
                    ds = ds.map(row_udf, fn_kwargs={"marker": str(marker)})
                else:
                    ds = ds.map_batches(
                        batch_udf,
                        batch_size=32,
                        fn_kwargs={"marker": str(marker)},
                    )
                plan = io.StringIO()
                if callable(getattr(ds, "explain", None)):
                    with redirect_stdout(plan):
                        returned = ds.explain()
                    if returned is not None:
                        plan.write(str(returned))
                else:
                    plan.write(str(ds))
                lazy_stats = ds.stats()
                start = time.perf_counter()
                measured = ds.materialize()
                elapsed = time.perf_counter() - start
                executed_calls = invocation_count(marker)
                rows = measured.take_all()
                reexecuted_calls = invocation_count(marker)
                if reexecuted_calls != executed_calls:
                    raise ValueError(
                        "reading materialized data re-executed the pipeline"
                    )
                actual = {row["id"]: row["value"] for row in rows}
                if len(rows) != 256 or actual != {n: n**2 for n in range(256)}:
                    raise ValueError("ETL output mismatch")
                stats = measured.stats()
                if not stats.strip():
                    raise ValueError(
                        "measured.stats() was empty after materialize()"
                    )
                stem = f"{mode}-{index}"
                (output / f"{stem}.plan.txt").write_text(
                    plan.getvalue(), encoding="utf-8"
                )
                (output / f"{stem}.stats.txt").write_text(
                    stats, encoding="utf-8"
                )
                (output / f"{stem}.correctness.json").write_text(
                    json.dumps(
                        {
                            "rows": len(rows),
                            "correct": True,
                            "udf_calls_at_materialize": executed_calls,
                            "udf_calls_after_read": reexecuted_calls,
                            "no_second_execution": True,
                            "lazy_ds_stats_empty": not lazy_stats.strip(),
                        }
                    ),
                    encoding="utf-8",
                )
                documents[mode]["samples"].append(
                    {
                        "value": elapsed,
                        "success": True,
                        "correct": True,
                        "evidence": (
                            f"{stem}.stats.txt + {stem}.correctness.json"
                        ),
                    }
                )
                result["verified_rounds"] += 1
        for mode, document in documents.items():
            (output / f"{mode}.json").write_text(
                json.dumps(document, indent=2), encoding="utf-8"
            )
        comparator = load("compare_runs")
        comparison = comparator.compare(
            documents["row"], documents["batch"], ("config",)
        )
        if not comparison["comparable"]:
            raise ValueError(
                "matching measurement contracts incorrectly rejected"
            )
        (output / "comparison.json").write_text(
            json.dumps(comparison, indent=2), encoding="utf-8"
        )
        result.update(
            success=True,
            rows_per_round=256,
            runtime=ray.__version__,
            python=platform.python_version(),
            stats_source="measured.stats() after ds.materialize()",
            no_second_execution=True,
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if ray is not None:
            ray.shutdown()
        session.cleanup()
        (output / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
