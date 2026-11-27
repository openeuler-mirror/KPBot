#!/usr/bin/env python3
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path


CONTRACT = "benchmark-wait-variance-decision/v1"
DETECTOR = "benchmark-wait-variance"
WAIT_FIELDS = ["runnable_delay_ms", "blocked_ms", "throttled_ms", "io_wait_ms", "downstream_wait_ms"]


class InputError(ValueError):
    pass


def positive(value, label, allow_zero=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InputError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        op = ">= 0" if allow_zero else "> 0"
        raise InputError(f"{label} must be finite and {op}")
    return value


def change(slow, fast):
    return (slow - fast) / fast


def med(values):
    return statistics.median(values)


def positive_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InputError(f"{label} must be an integer >= 1")
    return value


def write_json(path, payload, force):
    base = Path.cwd().resolve()
    requested = Path(path)
    target = Path(os.path.abspath(requested))
    if not target.name or target.name in {".", ".."}:
        raise InputError("output path must name a file")
    if not target.parent.exists() or not target.parent.is_dir():
        raise InputError("output parent directory does not exist")
    try:
        relative_parent = target.parent.relative_to(base)
    except ValueError as exc:
        raise InputError("output path must stay within the current working directory") from exc
    cursor = base
    for part in relative_parent.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InputError("output parent path must not contain symlinks")
    resolved_parent = target.parent.resolve()
    if resolved_parent != target.parent or not resolved_parent.is_relative_to(base):
        raise InputError("output parent path must resolve within the current working directory")
    target = resolved_parent / target.name
    if target.is_symlink():
        raise InputError("output path must not be a symlink")
    expected_stat = None
    if force and target.exists():
        if not target.is_file() or payload.get("run_id") in {None, "", "unknown"}:
            raise InputError("--force may only replace a prior decision from the same run")
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError("--force target is not a readable decision JSON") from exc
        identity = ("contract", "detector", "run_id")
        if not isinstance(existing, dict) or any(existing.get(key) != payload.get(key) for key in identity):
            raise InputError("--force target does not match this detector and run")
        expected_stat = target.stat(follow_symlinks=False)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.tmp.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if force and expected_stat is not None:
            current_stat = target.stat(follow_symlinks=False)
            expected_identity = (expected_stat.st_dev, expected_stat.st_ino, expected_stat.st_size, expected_stat.st_mtime_ns)
            current_identity = (current_stat.st_dev, current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns)
            if current_identity != expected_identity:
                raise InputError("--force target changed while the decision was being written")
            os.replace(temporary, target)
            temporary = None
        else:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise InputError("output exists; pass --force to overwrite") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def decision(run_id, status, confidence, evidence, required=None):
    return {
        "contract": CONTRACT,
        "detector": DETECTOR,
        "run_id": run_id,
        "status": status,
        "confidence": confidence,
        "evidence": evidence,
        "required_evidence": required or [],
    }


def classify(data):
    if not isinstance(data, dict):
        raise InputError("input must be a JSON object")
    if data.get("contract") != "benchmark-comparison/v1":
        raise InputError("unsupported input contract")
    run_id = str(data.get("run_id", "unknown"))
    thresholds = data.get("thresholds")
    work_metric = data.get("work_metric")
    samples = data.get("samples")
    model = data.get("execution_model")
    if model not in {"single-threaded", "multi-threaded"}:
        raise InputError("execution_model must be single-threaded or multi-threaded")
    if not isinstance(thresholds, dict) or not isinstance(work_metric, dict) or not isinstance(samples, list):
        raise InputError("thresholds, work_metric, and samples are required")
    min_samples = positive_integer(thresholds.get("min_samples_per_cohort"), "min_samples_per_cohort")
    material = positive(thresholds.get("material_change_ratio"), "material_change_ratio")
    equivalent = positive(thresholds.get("equivalence_ratio"), "equivalence_ratio")
    min_explained = positive(thresholds.get("min_explained_fraction"), "min_explained_fraction")
    if material <= equivalent or min_explained > 1:
        raise InputError("threshold relationships are invalid")
    field = work_metric.get("field")
    if not isinstance(field, str) or not field:
        raise InputError("work_metric.field is required")

    cohorts = {"fast": [], "slow": []}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict) or sample.get("cohort") not in cohorts:
            raise InputError(f"samples[{index}].cohort must be fast or slow")
        cohorts[sample["cohort"]].append(sample)
    if any(len(group) < min_samples for group in cohorts.values()):
        return decision(run_id, "blocked", "low", {"reason": "insufficient cohort samples"}, [f"at least {min_samples} fast and slow samples"])

    cpu_field = "task_clock_ms" if model == "single-threaded" else "critical_path_cpu_ms"
    required_fields = [field, "elapsed_ms", "instructions", cpu_field]
    if any(any(name not in sample for name in required_fields) for sample in samples):
        return decision(run_id, "blocked", "low", {"reason": "missing critical-path metric", "required_field": cpu_field}, required_fields)

    rows = {"fast": [], "slow": []}
    for name, group in cohorts.items():
        for sample in group:
            work = positive(sample[field], f"{name}.{field}")
            elapsed = positive(sample["elapsed_ms"], f"{name}.elapsed_ms")
            instructions = positive(sample["instructions"], f"{name}.instructions")
            cpu = positive(sample[cpu_field], f"{name}.{cpu_field}")
            if cpu > elapsed:
                raise InputError(f"{name}.{cpu_field} exceeds elapsed_ms")
            row = {
                "work": work,
                "elapsed_ms": elapsed,
                "instructions_per_work": instructions / work,
                "cpu_ms_per_work": cpu / work,
                "non_running_ms": elapsed - cpu,
            }
            for wait_field in WAIT_FIELDS:
                if wait_field in sample:
                    row[wait_field] = positive(sample[wait_field], f"{name}.{wait_field}", allow_zero=True)
            rows[name].append(row)

    base_keys = ["work", "elapsed_ms", "instructions_per_work", "cpu_ms_per_work", "non_running_ms"]
    medians = {key: {name: med([row[key] for row in cohort]) for name, cohort in rows.items()} for key in base_keys}
    gaps = {
        "work": change(medians["work"]["slow"], medians["work"]["fast"]),
        "elapsed": change(medians["elapsed_ms"]["slow"], medians["elapsed_ms"]["fast"]),
        "instructions_per_work": change(medians["instructions_per_work"]["slow"], medians["instructions_per_work"]["fast"]),
        "cpu_ms_per_work": change(medians["cpu_ms_per_work"]["slow"], medians["cpu_ms_per_work"]["fast"]),
    }
    extra_elapsed = medians["elapsed_ms"]["slow"] - medians["elapsed_ms"]["fast"]
    extra_wait = medians["non_running_ms"]["slow"] - medians["non_running_ms"]["fast"]
    explained = extra_wait / extra_elapsed if extra_elapsed > 0 else 0.0
    wait_increase_vs_fast_elapsed = extra_wait / medians["elapsed_ms"]["fast"]

    wait_deltas = {}
    for wait_field in WAIT_FIELDS:
        if all(wait_field in row for cohort in rows.values() for row in cohort):
            values = {name: med([row[wait_field] for row in cohort]) for name, cohort in rows.items()}
            wait_deltas[wait_field] = values["slow"] - values["fast"]
    positive_waits = {name: value for name, value in wait_deltas.items() if value > 0}
    wait_class = max(positive_waits, key=positive_waits.get) if positive_waits else "unattributed"

    evidence = {
        "cpu_field": cpu_field,
        "medians": medians,
        "relative_gaps": gaps,
        "extra_elapsed_ms": extra_elapsed,
        "extra_non_running_ms": extra_wait,
        "wait_increase_vs_fast_elapsed": wait_increase_vs_fast_elapsed,
        "extra_wait_explained_fraction": explained,
        "wait_class": wait_class,
        "wait_field_deltas_ms": wait_deltas,
        "thresholds": thresholds,
    }

    if abs(gaps["work"]) > equivalent or abs(gaps["instructions_per_work"]) > equivalent:
        return decision(run_id, "not_supported", "high", evidence, ["freeze workload and executed path before wait attribution"])
    if abs(gaps["cpu_ms_per_work"]) > equivalent:
        if gaps["cpu_ms_per_work"] < 0:
            evidence["mixed_change"] = "critical_path_cpu_cost_decreased_while_wait_increased"
            return decision(
                run_id,
                "inconclusive",
                "low",
                evidence,
                ["repeat with critical-path CPU and runnable/blocked intervals from the same window"],
            )
        return decision(run_id, "not_supported", "high", evidence, ["run CPU-efficiency classification"])
    if gaps["elapsed"] >= material and wait_increase_vs_fast_elapsed >= material and explained >= min_explained:
        confidence = "high" if wait_class != "unattributed" else "medium"
        return decision(run_id, "confirmed", confidence, evidence)
    if gaps["elapsed"] < material:
        return decision(run_id, "not_supported", "medium", evidence)
    return decision(run_id, "inconclusive", "low", evidence, ["critical-path runnable/blocked breakdown from the same benchmark window"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        input_path = Path(args.input)
        output_path = Path(args.output)
        if input_path.resolve() == output_path.resolve():
            raise InputError("input and output paths must differ")
        payload = classify(json.loads(input_path.read_text(encoding="utf-8")))
        write_json(output_path, payload, args.force)
    except (OSError, json.JSONDecodeError, InputError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
