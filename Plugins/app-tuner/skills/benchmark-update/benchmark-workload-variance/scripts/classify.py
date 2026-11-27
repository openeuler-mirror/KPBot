#!/usr/bin/env python3
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path


CONTRACT = "benchmark-workload-variance-decision/v1"
DETECTOR = "benchmark-workload-variance"


class InputError(ValueError):
    pass


def finite_positive(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InputError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise InputError(f"{label} must be finite and > 0")
    return value


def relative_change(slow, fast):
    return (slow - fast) / fast


def median(values):
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


def blocked(run_id, reason, required):
    return {
        "contract": CONTRACT,
        "detector": DETECTOR,
        "run_id": run_id,
        "status": "blocked",
        "confidence": "low",
        "evidence": {"reason": reason},
        "required_evidence": required,
    }


def classify(data):
    if not isinstance(data, dict):
        raise InputError("input must be a JSON object")
    if data.get("contract") != "benchmark-comparison/v1":
        raise InputError("unsupported input contract")
    run_id = str(data.get("run_id", "unknown"))
    benchmark_mode = data.get("benchmark_mode")
    if benchmark_mode not in {"fixed-work", "fixed-duration"}:
        raise InputError("benchmark_mode must be fixed-work or fixed-duration")
    thresholds = data.get("thresholds")
    work_metric = data.get("work_metric")
    samples = data.get("samples")
    if not isinstance(thresholds, dict) or not isinstance(work_metric, dict) or not isinstance(samples, list):
        raise InputError("thresholds, work_metric, and samples are required")

    min_samples = positive_integer(thresholds.get("min_samples_per_cohort"), "min_samples_per_cohort")
    material = finite_positive(thresholds.get("material_change_ratio"), "material_change_ratio")
    equivalent = finite_positive(thresholds.get("equivalence_ratio"), "equivalence_ratio")
    if material <= equivalent:
        raise InputError("material_change_ratio must exceed equivalence_ratio")

    field = work_metric.get("field")
    if not isinstance(field, str) or not field:
        raise InputError("work_metric.field is required")
    if work_metric.get("causal_role") != "independent":
        return blocked(
            run_id,
            "work metric is an outcome, not an independent workload measure",
            ["offered load, input size/mix, or another independently fixed work metric"],
        )

    cohorts = {"fast": [], "slow": []}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict) or sample.get("cohort") not in cohorts:
            raise InputError(f"samples[{index}].cohort must be fast or slow")
        cohorts[sample["cohort"]].append(sample)
    if any(len(values) < min_samples for values in cohorts.values()):
        return blocked(run_id, "insufficient cohort samples", [f"at least {min_samples} fast and slow samples"])

    try:
        work = {name: [finite_positive(s[field], f"{name}.{field}") for s in group] for name, group in cohorts.items()}
        elapsed = {name: [finite_positive(s["elapsed_ms"], f"{name}.elapsed_ms") for s in group] for name, group in cohorts.items()}
    except KeyError as exc:
        return blocked(run_id, f"missing required sample field: {exc.args[0]}", [field, "elapsed_ms"])

    per_work_elapsed = {
        name: [elapsed[name][i] / work[name][i] for i in range(len(work[name]))]
        for name in cohorts
    }
    instructions_available = all("instructions" in s for s in samples)
    per_work_instructions = None
    if instructions_available:
        per_work_instructions = {
            name: [finite_positive(s["instructions"], f"{name}.instructions") / work[name][i] for i, s in enumerate(group)]
            for name, group in cohorts.items()
        }

    med = {
        "work": {name: median(values) for name, values in work.items()},
        "elapsed_ms": {name: median(values) for name, values in elapsed.items()},
        "elapsed_ms_per_work": {name: median(values) for name, values in per_work_elapsed.items()},
    }
    if per_work_instructions:
        med["instructions_per_work"] = {name: median(values) for name, values in per_work_instructions.items()}

    gaps = {
        "work": relative_change(med["work"]["slow"], med["work"]["fast"]),
        "elapsed": relative_change(med["elapsed_ms"]["slow"], med["elapsed_ms"]["fast"]),
        "elapsed_per_work": relative_change(
            med["elapsed_ms_per_work"]["slow"], med["elapsed_ms_per_work"]["fast"]
        ),
    }
    if per_work_instructions:
        gaps["instructions_per_work"] = relative_change(
            med["instructions_per_work"]["slow"], med["instructions_per_work"]["fast"]
        )

    if gaps["work"] >= material and gaps["elapsed"] >= material and abs(gaps["elapsed_per_work"]) <= equivalent:
        status = "confirmed"
        confidence = "high" if "instructions_per_work" in gaps and abs(gaps["instructions_per_work"]) <= equivalent else "medium"
        required = []
    elif abs(gaps["work"]) <= equivalent:
        status = "not_supported"
        confidence = "high"
        required = []
    else:
        status = "inconclusive"
        confidence = "low"
        required = ["repeat with an independently fixed input manifest and explicit retry/path counters"]

    return {
        "contract": CONTRACT,
        "detector": DETECTOR,
        "run_id": run_id,
        "status": status,
        "confidence": confidence,
        "evidence": {"medians": med, "relative_gaps": gaps, "thresholds": thresholds},
        "required_evidence": required,
    }


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
        data = json.loads(input_path.read_text(encoding="utf-8"))
        decision = classify(data)
        write_json(output_path, decision, args.force)
    except (OSError, json.JSONDecodeError, InputError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
