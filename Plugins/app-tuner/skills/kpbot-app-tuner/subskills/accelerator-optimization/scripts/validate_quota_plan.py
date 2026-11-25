#!/usr/bin/env python3
"""Validate ASF quota changes against per-algorithm dev_metadata throughput.

Usage: validate_quota_plan.py <scheduler.json> <uadk_engine.json> <quota> <all-quotas 0|1>

The scheduler may contain multiple algorithms/devices.  `--only-quota` rewrites
the first quota (or every quota with --all-quotas), so every entry that will be
changed must have a comparable throughput and must not exceed it.  Missing
metadata, unknown algorithms, incomparable units and over-limit values are all
hard errors (exit 2) before the caller writes anything.
"""
import json
import re
import sys

CAPACITY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(OPS|GB/s|MB/s|KB/s)$")
MULTIPLIER = {"OPS": None, "GB/s": 1024 ** 3, "MB/s": 1024 ** 2, "KB/s": 1024}


def capacity(value, label):
    text = str(value or "").strip()
    match = CAPACITY_RE.fullmatch(text)
    if not match:
        raise ValueError(f"{label} has invalid capacity value: {value!r}")
    number, unit = match.groups()
    if unit == "OPS":
        return ("ops", float(number))
    return ("bps", float(number) * MULTIPLIER[unit])


def load(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"cannot parse {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} {path} must contain a JSON object")
    return data


def throughput_map(metadata):
    """(SN, processor) -> (dimension, value), keeping the strictest limit."""
    limits = {}
    processors = metadata.get("processor")
    if not isinstance(processors, list):
        raise ValueError("dev_metadata has no processor list")
    for processor in processors:
        if not isinstance(processor, dict):
            continue
        pname = processor.get("name")
        algorithms = processor.get("algorithm")
        if not isinstance(algorithms, list):
            continue
        for algorithm in algorithms:
            if not isinstance(algorithm, dict):
                continue
            sn = algorithm.get("SN")
            value = algorithm.get("throughput")
            if not sn or value in (None, ""):
                continue
            parsed = capacity(value, f"throughput for SN={sn!r} processor={pname!r}")
            key = (str(sn), str(pname))
            current = limits.get(key)
            if current is None or parsed[0] != current[0] or parsed[1] < current[1]:
                limits[key] = parsed
    return limits


def main():
    if len(sys.argv) != 5:
        print("error: scheduler/dev_metadata/quota/all-quotas required", file=sys.stderr)
        return 2
    scheduler_path, metadata_path, quota_raw, all_quotas_raw = sys.argv[1:5]
    all_quotas = all_quotas_raw == "1"
    try:
        scheduler = load(scheduler_path, "scheduler.json")
        metadata = load(metadata_path, "dev_metadata/uadk_engine.json")
        limits = throughput_map(metadata)
        entries = scheduler.get("scheduler")
        if not isinstance(entries, list) or not entries:
            raise ValueError("scheduler.json has no scheduler entries")
        changed = 0
        for si, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"scheduler[{si}] is not an object")
            sn = entry.get("SN")
            devices = entry.get("sched_dev")
            if not isinstance(devices, list) or not devices:
                raise ValueError(f"scheduler[{si}] SN={sn!r} has no sched_dev entries")
            for di, device in enumerate(devices):
                if not all_quotas and (si, di) != (0, 0):
                    continue
                if not isinstance(device, dict):
                    raise ValueError(f"scheduler[{si}].sched_dev[{di}] is not an object")
                device_quota = quota_raw if (all_quotas or (si, di) == (0, 0)) else device.get("quota")
                if device_quota in (None, ""):
                    raise ValueError(
                        f"scheduler[{si}].sched_dev[{di}] SN={sn!r} has no quota"
                    )
                pname = device.get("processor")
                dname = device.get("name")
                target = limits.get((str(sn), str(pname)))
                if target is None:
                    target = limits.get((str(sn), str(dname)))
                if target is None:
                    candidates = [k for k in limits if k[0] == str(sn)]
                    if len(candidates) == 1:
                        target = limits[candidates[0]]
                if target is None:
                    raise ValueError(
                        f"no throughput for SN={sn!r} processor={pname!r} device={dname!r}"
                    )
                actual = capacity(device_quota, f"quota for SN={sn!r} device={dname!r}")
                if actual[0] != target[0]:
                    raise ValueError(
                        f"quota {device_quota!r} and throughput use incomparable units "
                        f"for SN={sn!r} device={dname!r}"
                    )
                if actual[1] > target[1]:
                    raise ValueError(
                        f"quota {device_quota!r} exceeds throughput for SN={sn!r} "
                        f"device={dname!r}"
                    )
                changed += 1
        if changed == 0:
            raise ValueError("scheduler.json has no quota entries to rewrite")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
