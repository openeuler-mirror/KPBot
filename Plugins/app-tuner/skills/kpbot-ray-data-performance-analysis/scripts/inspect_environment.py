#!/usr/bin/env python3
"""Read a local resource snapshot without importing Ray or connecting to it."""

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def cgroup_locations(membership, mountinfo):
    """Resolve membership against mounts; never guess host filesystem paths."""
    groups = []
    for line in membership.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            groups.append((set(filter(None, parts[1].split(","))), parts[2]))
    locations = []
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields, extra = left.split(), right.split()
        if not sep or len(fields) < 5 or len(extra) < 3:
            continue
        kind = extra[0]
        if kind not in ("cgroup", "cgroup2"):
            continue
        controllers = set(extra[2].split(","))
        root = PurePosixPath(unescape_mount(fields[3]))
        mount = Path(unescape_mount(fields[4]))
        for names, group in groups:
            if (kind == "cgroup2" and names) or (
                kind == "cgroup"
                and "cpu" not in names.intersection(controllers)
            ):
                continue
            try:
                relative = PurePosixPath(group).relative_to(root)
            except ValueError:
                continue
            if ".." in relative.parts:
                continue
            locations.append((kind, mount.joinpath(*relative.parts), mount))
    return locations


def quota_value(kind, first, second=None):
    try:
        if kind == "cgroup2":
            quota, period = first.split()
            if quota == "max":
                return None
        else:
            quota, period = first, second
        quota, period = int(quota), int(period)
        return quota / period if quota > 0 and period > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def cgroup_snapshot(membership, mountinfo, reader=read_text):
    result = {"groups": [], "cpu_quota_upper_bound": None}
    limits = []
    for kind, current, mount in cgroup_locations(membership, mountinfo):
        group = {"kind": kind, "path": str(current), "quota_chain": []}
        node = current
        while True:
            if kind == "cgroup2":
                raw = reader(node / "cpu.max")
                limit = quota_value(kind, raw)
            else:
                raw = reader(node / "cpu.cfs_quota_us")
                period = reader(node / "cpu.cfs_period_us")
                limit = quota_value(kind, raw, period)
                raw = {"quota": raw, "period": period}
            group["quota_chain"].append(
                {"path": str(node), "raw": raw, "cpus": limit}
            )
            if limit is not None:
                limits.append(limit)
            if node == mount:
                break
            node = node.parent
        if kind == "cgroup2":
            for name in (
                "cpuset.cpus.effective",
                "cpuset.mems.effective",
                "cpu.stat",
                "memory.max",
                "memory.current",
            ):
                group[name] = reader(current / name)
        result["groups"].append(group)
    if limits:
        result["cpu_quota_upper_bound"] = min(limits)
    return result


def collect():
    result = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "local_process_and_visible_cgroups_only",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "packages": {},
        "cpu": {
            "os_cpu_count": os.cpu_count(),
            "affinity": None,
            "visible_cpu_upper_bound": None,
        },
        "cgroup": None,
        "shared_memory": None,
        "memory": None,
        "cpu_identity": None,
        "numa_online": None,
        "tools": {
            name: shutil.which(name) is not None
            for name in ("perf", "py-spy", "pidstat", "nmon", "nvidia-smi")
        },
        "limitations": [
            "Snapshot, not utilization; no remote worker or GPU runtime probe."
        ],
    }
    for package in ("ray", "numpy", "pandas", "pyarrow", "torch"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    if hasattr(os, "sched_getaffinity"):
        try:
            result["cpu"]["affinity"] = sorted(os.sched_getaffinity(0))
        except OSError as error:
            result["limitations"].append(f"Affinity unavailable: {error}")
    if platform.system() == "Linux":
        cpuinfo = read_text("/proc/cpuinfo")
        if cpuinfo:
            identity = {}
            for line in cpuinfo.splitlines():
                key, separator, value = line.partition(":")
                key = key.strip().lower()
                if separator and key in (
                    "model name",
                    "cpu implementer",
                    "cpu part",
                    "features",
                    "flags",
                ):
                    identity.setdefault(key, set()).add(value.strip())
            result["cpu_identity"] = {
                key: sorted(values) for key, values in identity.items()
            }
        meminfo = read_text("/proc/meminfo")
        if meminfo:
            result["memory"] = {}
            for line in meminfo.splitlines():
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    result["memory"][key] = value.strip()
        membership = read_text("/proc/self/cgroup")
        mounts = read_text("/proc/self/mountinfo")
        if membership is not None and mounts is not None:
            result["cgroup"] = cgroup_snapshot(membership, mounts)
            if not result["cgroup"]["groups"]:
                result["limitations"].append(
                    "No resolvable visible CPU cgroup mount."
                )
        else:
            result["limitations"].append(
                "cgroup membership or mountinfo unavailable."
            )
        result["numa_online"] = read_text("/sys/devices/system/node/online")
        try:
            usage = shutil.disk_usage("/dev/shm")
            result["shared_memory"] = {
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            }
        except OSError:
            result["limitations"].append("/dev/shm unavailable.")
        result["limitations"].append(
            "Hidden ancestor quotas and resource contention unknown."
        )
        result["limitations"].append(
            "/proc/meminfo may describe host memory, not container limits."
        )
    else:
        result["limitations"].append(
            "Linux cgroup, NUMA and /dev/shm probes unavailable."
        )
    bounds = []
    if result["cpu"]["affinity"] is not None:
        bounds.append(len(result["cpu"]["affinity"]))
    quota = (result["cgroup"] or {}).get("cpu_quota_upper_bound")
    if quota is not None:
        bounds.append(quota)
    result["cpu"]["visible_cpu_upper_bound"] = min(bounds) if bounds else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(
                collect(),
                output,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            output.write("\n")
    except OSError as error:
        parser.exit(1, f"Cannot write new snapshot: {error}\n")


if __name__ == "__main__":
    main()
