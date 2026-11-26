#!/usr/bin/env python3
"""Read-only Ray Data local prerequisite probes; never starts or stops Ray."""

import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys

EXPECTED = {"ray": "2.56.0", "numpy": "2.1.3", "pyarrow": "23.0.1"}
MIN_CPUS = 2
MIN_SHM = 256 * 1024 * 1024


def probe(name, status, evidence, reason=None, mandatory=True):
    return {
        "name": name,
        "status": status,
        "mandatory": mandatory,
        "reason": reason,
        "evidence": evidence,
    }


def ready(name, evidence):
    return probe(name, "ready", evidence)


def blocked(name, reason, evidence):
    return probe(name, "blocked", evidence, reason)


def unknown(name, reason, evidence):
    return probe(name, "unknown", evidence, reason)


def error_data(error):
    return {"error_type": type(error).__name__, "message": str(error)[:500]}


def platform_probe():
    evidence = {"expected": "Linux", "actual": platform.system()}
    if platform.system() != "Linux":
        return blocked("linux", "unsupported_platform", evidence)
    return ready("linux", evidence)


def python_probe():
    evidence = {"expected": "3.11", "actual": platform.python_version()}
    if sys.version_info[:2] != (3, 11):
        return blocked("python", "wrong_version", evidence)
    return ready("python", evidence)


def package_probe(package):
    evidence = {
        "package": package,
        "expected": EXPECTED[package],
        "actual": None,
    }
    try:
        actual = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return blocked(package + "_version", "not_installed", evidence)
    except (ValueError, OSError) as error:
        evidence.update(error_data(error))
        return unknown(package + "_version", "metadata_unreadable", evidence)
    evidence["actual"] = actual
    if actual != EXPECTED[package]:
        return blocked(package + "_version", "wrong_version", evidence)
    return ready(package + "_version", evidence)


def _mount_path(value):
    """Decode the octal escapes used by proc mountinfo paths."""
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _path_below(path, root):
    if root == "/":
        return path.lstrip("/")
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


def cgroup_directories(controller, proc_root=Path("/")):
    """Return the current group and visible ancestors for one controller."""
    try:
        records = (
            proc_root / "proc/self/cgroup"
        ).read_text(encoding="ascii").splitlines()
        mountinfo = (
            proc_root / "proc/self/mountinfo"
        ).read_text(encoding="ascii").splitlines()
    except OSError:
        return []
    memberships = []
    for line in records:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        controllers = fields[1].split(",") if fields[1] else []
        version = 2 if not controllers else 1
        if version == 2 or controller in controllers:
            memberships.append((version, fields[2]))
    candidates = []
    for line in mountinfo:
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            filesystem, _source, options = right.split()[:3]
            version = 2 if filesystem == "cgroup2" else 1
            if filesystem not in ("cgroup", "cgroup2"):
                continue
            if version == 1 and controller not in options.split(","):
                continue
            root = _mount_path(fields[3])
            mount_point = _mount_path(fields[4])
        except (ValueError, IndexError):
            continue
        for member_version, member_path in memberships:
            if member_version != version:
                continue
            relative = _path_below(member_path, root)
            if relative is None:
                # In a cgroup namespace the membership path is already
                # relative to its root, while mountinfo can retain the
                # original, deeper mount root.
                relative = member_path.lstrip("/")
            candidates.append((len(root), version, root, mount_point, relative))
    if not candidates:
        return []
    _length, version, _root, mount_point, relative = max(candidates)
    base = proc_root / mount_point.lstrip("/")
    current = base / relative if relative else base
    groups = [current]
    while current != base:
        current = current.parent
        groups.append(current)
    return [(version, group) for group in groups]


def cgroup_cpu_quotas(proc_root=Path("/")):
    quotas = []
    for version, group in cgroup_directories("cpu", proc_root):
        try:
            if version == 2:
                raw = (group / "cpu.max").read_text(encoding="ascii").split()
                unlimited = raw[0] == "max"
                quota = -1 if unlimited else int(raw[0])
                period = int(raw[1])
            else:
                quota = int(
                    (group / "cpu.cfs_quota_us")
                    .read_text(encoding="ascii")
                    .strip()
                )
                period = int(
                    (group / "cpu.cfs_period_us")
                    .read_text(encoding="ascii")
                    .strip()
                )
                unlimited = quota < 0
            if not unlimited and quota > 0 and period > 0:
                quotas.append(quota / period)
        except (OSError, ValueError, IndexError):
            continue
    return quotas


def cgroup_memory_remaining(proc_root=Path("/")):
    remaining = []
    for version, group in cgroup_directories("memory", proc_root):
        try:
            if version == 2:
                raw_maximum = (group / "memory.max").read_text(
                    encoding="ascii"
                ).strip()
                maximum = None if raw_maximum == "max" else int(raw_maximum)
                current = int(
                    (group / "memory.current")
                    .read_text(encoding="ascii")
                    .strip()
                )
            else:
                maximum = int(
                    (group / "memory.limit_in_bytes")
                    .read_text(encoding="ascii")
                    .strip()
                )
                current = int(
                    (group / "memory.usage_in_bytes")
                    .read_text(encoding="ascii")
                    .strip()
                )
                if maximum >= 1 << 60:
                    maximum = None
            if maximum is not None:
                remaining.append(max(0, maximum - current))
        except (OSError, ValueError):
            continue
    return remaining


def visible_cpus(proc_root=Path("/")):
    evidence = {
        "affinity": None,
        "os_count": os.cpu_count(),
        "cgroup_quota": None,
        "cgroup_quotas": [],
    }
    bounds = []
    try:
        evidence["affinity"] = len(os.sched_getaffinity(0))
        bounds.append(evidence["affinity"])
    except (AttributeError, OSError):
        pass
    quotas = cgroup_cpu_quotas(proc_root)
    evidence["cgroup_quotas"] = quotas
    if quotas:
        evidence["cgroup_quota"] = min(quotas)
        bounds.extend(quotas)
    if evidence["os_count"]:
        bounds.append(evidence["os_count"])
    visible = min(bounds) if bounds else None
    evidence.update(minimum=MIN_CPUS, visible_upper_bound=visible)
    if visible is None:
        return unknown("cpus", "cpu_visibility_unknown", evidence)
    if visible < MIN_CPUS:
        return blocked("cpus", "insufficient_cpus", evidence)
    return ready("cpus", evidence)


def memory_probe(proc_root=Path("/")):
    evidence = {
        "minimum_available_bytes": MIN_SHM,
        "mem_available_bytes": None,
        "cgroup_remaining_bytes": None,
        "cgroup_remaining_by_group_bytes": [],
    }
    available = []
    try:
        for line in (
            proc_root / "proc/meminfo"
        ).read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                evidence["mem_available_bytes"] = int(line.split()[1]) * 1024
                available.append(evidence["mem_available_bytes"])
                break
    except (OSError, ValueError, IndexError):
        pass
    remaining = cgroup_memory_remaining(proc_root)
    evidence["cgroup_remaining_by_group_bytes"] = remaining
    if remaining:
        evidence["cgroup_remaining_bytes"] = min(remaining)
        available.extend(remaining)
    if not available:
        return unknown("finite_memory", "memory_visibility_unknown", evidence)
    evidence["available_upper_bound_bytes"] = min(available)
    if min(available) < MIN_SHM:
        return blocked(
            "finite_memory", "insufficient_available_memory", evidence
        )
    return ready("finite_memory", evidence)


def shm_probe():
    try:
        usage = shutil.disk_usage("/dev/shm")
    except OSError as error:
        return unknown("free_shm", "shm_unreadable", error_data(error))
    evidence = {
        "path": "/dev/shm",
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "minimum_free_bytes": MIN_SHM,
    }
    if usage.total < MIN_SHM or usage.free < MIN_SHM:
        return blocked("free_shm", "insufficient_shm", evidence)
    return ready("free_shm", evidence)


def native_operations_probe():
    evidence = {
        "operations": ["numpy.arange*2+1", "pyarrow.Table.from_arrays"]
    }
    try:
        np = importlib.import_module("numpy")
        pa = importlib.import_module("pyarrow")
        values = np.arange(8, dtype=np.int64) * 2 + 1
        table = pa.Table.from_arrays([pa.array(values)], names=["value"])
        actual = table.column("value").to_pylist()
        expected = list(range(1, 16, 2))
        evidence.update(actual=actual, expected=expected)
        if actual != expected:
            return blocked("native_operations", "incorrect_result", evidence)
    except Exception as error:
        evidence.update(error_data(error))
        return blocked("native_operations", "operation_failed", evidence)
    return ready("native_operations", evidence)


def ray_api_probe():
    evidence = {"ray_init_called": False, "checked": []}
    try:
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
        for name, (function, parameters) in required.items():
            if not callable(function):
                raise AttributeError(name + " is not callable")
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
            if missing:
                evidence.update(symbol=name, missing_parameters=missing)
                return blocked(
                    "ray_data_api", "incompatible_signature", evidence
                )
            evidence["checked"].append(name)
    except Exception as error:
        evidence.update(error_data(error))
        return blocked("ray_data_api", "api_unavailable", evidence)
    return ready("ray_data_api", evidence)


def output_probe(path):
    evidence = {"path": str(path), "must_not_exist": True}
    if os.path.lexists(path):
        return blocked("output", "already_exists", evidence)
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        evidence["parent_writable"] = os.access(parent, os.W_OK | os.X_OK)
    except OSError as error:
        evidence.update(error_data(error))
        return blocked("output", "parent_unwritable", evidence)
    if not evidence["parent_writable"]:
        return blocked("output", "parent_unwritable", evidence)
    return ready("output", evidence)


def collect(output):
    probes = [
        platform_probe(),
        python_probe(),
        visible_cpus(),
        memory_probe(),
        shm_probe(),
    ]
    probes.extend(package_probe(package) for package in EXPECTED)
    prerequisites = all(item["status"] == "ready" for item in probes)
    if prerequisites:
        probes.extend([native_operations_probe(), ray_api_probe()])
    else:
        evidence = {"prerequisite": "platform, resources and exact packages"}
        probes.extend(
            [
                unknown(
                    "native_operations", "prerequisite_not_ready", evidence
                ),
                unknown("ray_data_api", "prerequisite_not_ready", evidence),
            ]
        )
    probes.append(output_probe(output))
    mandatory = [item for item in probes if item["mandatory"]]
    if any(item["status"] == "blocked" for item in mandatory):
        status = "blocked"
    elif any(item["status"] == "unknown" for item in mandatory):
        status = "unknown"
    else:
        status = "ready"
    return {
        "schema_version": 1,
        "status": status,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "existing_local_runtime_only",
        "probes": probes,
        "summary": {
            name: sum(item["status"] == name for item in probes)
            for name in ("ready", "blocked", "unknown")
        },
        "limitations": [
            "Local prerequisite evidence only; "
            "not production or cluster health.",
            "No ray.init, ray start or ray stop; "
            "imports and native operations only.",
        ],
    }


def write_exclusive(path, report):
    payload = (
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", required=True, type=Path, help="new JSON report"
    )
    args = parser.parse_args(argv)
    report = collect(args.output)
    try:
        write_exclusive(args.output, report)
    except OSError as error:
        print("cannot create exclusive report: " + str(error), file=sys.stderr)
        return 2
    print(
        json.dumps({"status": report["status"], "summary": report["summary"]})
    )
    return {"ready": 0, "blocked": 1, "unknown": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
