"""Collect real Ray evidence and compare six checked ETL runs."""

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / f"scripts/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row_udf(row):
    return {"id": row["id"], "value": row["id"] ** 2}


def batch_udf(batch):
    return {"id": batch["id"], "value": batch["id"] ** 2}


def main():
    output = ROOT / "results/real-ray"
    output.mkdir(parents=True, exist_ok=False)
    result = {"success": False, "verified_rounds": 0}
    session = tempfile.TemporaryDirectory(prefix="kp-analysis-")
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
            "scope": "dataset-construction-to-take-all",
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
                start = time.perf_counter()
                ds = ray.data.range(256, override_num_blocks=4)
                ds = (
                    ds.map(row_udf)
                    if mode == "row"
                    else ds.map_batches(batch_udf, batch_size=32)
                )
                plan = io.StringIO()
                if callable(getattr(ds, "explain", None)):
                    with redirect_stdout(plan):
                        returned = ds.explain()
                    if returned is not None:
                        plan.write(str(returned))
                else:
                    plan.write(str(ds))
                rows = ds.take_all()
                elapsed = time.perf_counter() - start
                actual = {row["id"]: row["value"] for row in rows}
                if len(rows) != 256 or actual != {n: n**2 for n in range(256)}:
                    raise ValueError("ETL output mismatch")
                stats = ds.stats()
                if not stats.strip():
                    raise ValueError("Ray returned empty Dataset stats")
                stem = f"{mode}-{index}"
                (output / f"{stem}.plan.txt").write_text(
                    plan.getvalue(), encoding="utf-8"
                )
                (output / f"{stem}.stats.txt").write_text(
                    stats, encoding="utf-8"
                )
                (output / f"{stem}.correctness.json").write_text(
                    json.dumps({"rows": len(rows), "correct": True}),
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
