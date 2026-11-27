#!/usr/bin/env python3
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path


CONTRACT = "benchmark-cpu-efficiency-variance-decision/v1"
DETECTOR = "benchmark-cpu-efficiency-variance"


class InputError(ValueError):
    pass


def number(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InputError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise InputError(f"{label} must be finite and > 0")
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
    if not isinstance(thresholds, dict) or not isinstance(work_metric, dict) or not isinstance(samples, list):
        raise InputError("thresholds, work_metric, and samples are required")
    min_samples = positive_integer(thresholds.get("min_samples_per_cohort"), "min_samples_per_cohort")
    material = number(thresholds.get("material_change_ratio"), "material_change_ratio")
    equivalent = number(thresholds.get("equivalence_ratio"), "equivalence_ratio")
    if material <= equivalent:
        raise InputError("material_change_ratio must exceed equivalence_ratio")
    field = work_metric.get("field")
    if not isinstance(field, str) or not field:
        raise InputError("work_metric.field is required")

    quality = data.get("counter_quality")
    if quality is not None and (not isinstance(quality, str) or not quality):
        raise InputError("counter_quality must be a non-empty string")
    if quality is None:
        return decision(
            run_id,
            "blocked",
            "low",
            {"counter_quality": None},
            ["an explicit counter_quality value backed by the collection window"],
        )
    if quality != "valid":
        status = "blocked" if quality == "unavailable" else "inconclusive"
        return decision(run_id, status, "low", {"counter_quality": quality}, ["valid non-multiplexed or quality-accounted CPU counters"])

    model = data.get("execution_model")
    if model not in {"single-threaded", "multi-threaded"}:
        raise InputError("execution_model must be single-threaded or multi-threaded")
    counter_scope = data.get("counter_scope")
    if counter_scope is not None and (not isinstance(counter_scope, str) or not counter_scope):
        raise InputError("counter_scope must be a non-empty string")
    if counter_scope not in {"critical-path", "process-aggregate", "cgroup-aggregate"}:
        return decision(
            run_id,
            "inconclusive",
            "low",
            {"counter_quality": quality, "execution_model": model, "counter_scope": counter_scope},
            ["an explicit CPU counter scope for the same benchmark window"],
        )
    context = {"counter_quality": quality, "execution_model": model, "counter_scope": counter_scope}
    if model == "multi-threaded" and counter_scope != "critical-path":
        return decision(
            run_id,
            "inconclusive",
            "low",
            context,
            ["critical-path task-clock, instructions, and cycles for the multi-threaded benchmark"],
        )

    cohorts = {"fast": [], "slow": []}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict) or sample.get("cohort") not in cohorts:
            raise InputError(f"samples[{index}].cohort must be fast or slow")
        cohorts[sample["cohort"]].append(sample)
    if any(len(group) < min_samples for group in cohorts.values()):
        return decision(run_id, "blocked", "low", {**context, "reason": "insufficient cohort samples"}, [f"at least {min_samples} fast and slow samples"])

    required_fields = [field, "elapsed_ms", "task_clock_ms", "instructions", "cycles"]
    if any(any(name not in sample for name in required_fields) for sample in samples):
        return decision(run_id, "blocked", "low", {**context, "reason": "missing required CPU metric"}, required_fields)

    derived = {"fast": [], "slow": []}
    for name, group in cohorts.items():
        for sample in group:
            work = number(sample[field], f"{name}.{field}")
            elapsed = number(sample["elapsed_ms"], f"{name}.elapsed_ms")
            task = number(sample["task_clock_ms"], f"{name}.task_clock_ms")
            instructions = number(sample["instructions"], f"{name}.instructions")
            cycles = number(sample["cycles"], f"{name}.cycles")
            derived[name].append({
                "work": work,
                "elapsed_ms": elapsed,
                "task_clock_ms_per_work": task / work,
                "instructions_per_work": instructions / work,
                "cpi": cycles / instructions,
                "effective_ghz": cycles / task / 1_000_000,
            })

    keys = ["work", "elapsed_ms", "task_clock_ms_per_work", "instructions_per_work", "cpi", "effective_ghz"]
    medians = {key: {name: med([row[key] for row in rows]) for name, rows in derived.items()} for key in keys}
    gaps = {key: change(values["slow"], values["fast"]) for key, values in medians.items()}

    if abs(gaps["work"]) > equivalent:
        return decision(run_id, "not_supported", "high", {**context, "medians": medians, "relative_gaps": gaps}, ["run workload-variance classification first"])
    if abs(gaps["instructions_per_work"]) > equivalent:
        return decision(run_id, "not_supported", "high", {**context, "medians": medians, "relative_gaps": gaps}, ["explain the additional executed instructions or changed code path"])
    if gaps["task_clock_ms_per_work"] < material:
        return decision(run_id, "not_supported", "high", {**context, "medians": medians, "relative_gaps": gaps}, ["evaluate off-CPU waiting if elapsed time still regresses"])

    mechanism = None
    if gaps["cpi"] >= material:
        mechanism = "cpi_increase"
    elif gaps["effective_ghz"] <= -material:
        mechanism = "effective_frequency_drop"

    if mechanism:
        return decision(
            run_id,
            "confirmed",
            "high" if gaps["elapsed_ms"] >= material else "medium",
            {**context, "mechanism": mechanism, "medians": medians, "relative_gaps": gaps, "thresholds": thresholds},
        )
    return decision(
        run_id,
        "inconclusive",
        "low",
        {**context, "medians": medians, "relative_gaps": gaps, "thresholds": thresholds},
        ["cache/memory stall, NUMA, thermal, frequency, or contention evidence from the same window"],
    )


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
