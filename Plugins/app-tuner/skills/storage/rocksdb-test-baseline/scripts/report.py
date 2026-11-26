#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

METRICS = {
    "Throughput(ops/sec)": ("throughput_ops_sec", float),
    "AverageLatency(us)": ("average_latency_us", float),
    "95thPercentileLatency(us)": ("p95_latency_us", float),
    "99thPercentileLatency(us)": ("p99_latency_us", float),
    "Operations": ("operations", int),
}
WORKLOAD_RE = re.compile(r"baseline_workload([a-f])(?:_fl\d+)?_.*\.log$")
BASELINE_WORKLOADS = "a b c d e f"
BUSINESS_SCOPES = {"READ", "UPDATE", "INSERT", "SCAN", "DELETE", "READ-MODIFY-WRITE", "APPEND"}


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def read_preflight_txt(result_dir):
    values = {}
    path = result_dir / "env/preflight.txt"
    if not path.exists():
        return values
    text = path.read_text(errors="replace")
    match = re.search(r"HugePages_Total:\s*(\d+)", text)
    if match:
        values["hugepages_total"] = match.group(1)
    match = re.search(r"HugePages_Free:\s*(\d+)", text)
    if match:
        values["hugepages_free"] = match.group(1)
    match = re.search(r"Hugepagesize:\s*(\d+\s*\w+)", text)
    if match:
        values["hugepagesize"] = match.group(1)
    for line in text.splitlines():
        if "madvise" in line and "always" in line and "never" in line:
            values["thp"] = line.strip()
            break
    return values


def parse_disk_collected(env):
    collected = env.get("collected") or {}
    fs_type = "unavailable"
    mount_options = "unavailable"
    for line in (collected.get("findmnt") or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] != "TARGET":
            fs_type = parts[2]
            mount_options = " ".join(parts[3:])
            break
    rota = "unavailable"
    model = str(env.get("disk_model") or "")
    for line in (collected.get("lsblk") or "").splitlines():
        parts = line.split()
        if model and len(parts) >= 5 and model in line:
            rota = parts[3]
            break
    return fs_type, mount_options, rota


def parse_ycsb(path):
    values = {"per_operation": {}, "return_errors": 0}
    if not path.exists():
        return values
    pattern = re.compile(r"^\[([A-Z-]+)\],\s*([^,]+),\s*(.+?)\s*$")
    operations = {}
    for line in path.read_text(errors="replace").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        scope = match.group(1)
        name = match.group(2).strip()
        raw = match.group(3).strip()
        if name.startswith("Return="):
            try:
                count = int(raw)
            except ValueError:
                continue
            if name != "Return=OK":
                values["return_errors"] += count
            continue
        if name not in METRICS:
            continue
        key, cast = METRICS[name]
        try:
            number = cast(raw)
        except ValueError:
            continue
        if scope == "OVERALL":
            values[key] = number
        elif scope in BUSINESS_SCOPES:
            operations.setdefault(scope, {})[key] = number

    values["per_operation"] = operations
    total_ops = sum(metrics.get("operations", 0) for metrics in operations.values())
    if "operations" not in values and total_ops:
        values["operations"] = total_ops
    if "average_latency_us" not in values and total_ops:
        weighted = sum(metrics.get("average_latency_us", 0) * metrics.get("operations", 0)
                       for metrics in operations.values())
        values["average_latency_us"] = weighted / total_ops
    return values


def collect(command, timeout=15):
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        output = (proc.stdout or "") + (proc.stderr or "")
        return output.strip() or "unavailable"
    except Exception:
        return "unavailable"


def environment(result_dir, data_dir, skip_env):
    pre = read_env(result_dir / "env/preflight.env")
    deps = read_env(result_dir / "env/deps.env")
    rocks = read_env(result_dir / "env/rocksdb.env")
    ycsb = read_env(result_dir / "env/ycsb.env")
    deploy = read_env(result_dir / "env/deploy.env")
    collected = {}
    cache_path = result_dir / "env/collected.json"
    if cache_path.exists():
        try:
            collected = json.loads(cache_path.read_text())
        except Exception:
            collected = {}
    if not collected and not skip_env:
        collected = {
            "uname": collect(["uname", "-a"]),
            "lscpu": collect(["lscpu"]),
            "free": collect(["free", "-h"]),
            "numactl": collect(["numactl", "-H"]),
            "lsblk": "\n".join(
                line for line in collect([
                    "lsblk", "-d", "-o", "NAME,TYPE,SIZE,ROTA,MODEL"
                ]).splitlines() if "nbd" not in line
            ) or "unavailable",
            "findmnt": collect(["findmnt", "-T", str(data_dir)]),
            "df": collect(["df", "-hT", str(data_dir)]),
            "java": collect(["java", "-version"]),
            "gcc": collect(["gcc", "--version"]),
            "mvn": collect(["mvn", "-version"]),
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(collected, indent=2, ensure_ascii=False))
        except Exception:
            pass
    env = {
        "arch": pre.get("ARCH", "unavailable"),
        "os": pre.get("OS", "unavailable"),
        "kernel": pre.get("KERNEL", "unavailable"),
        "cpu_model": pre.get("CPU_MODEL", "unavailable"),
        "cpu_list": pre.get("CPU_LIST", "unavailable"),
        "numa_node": pre.get("NUMA_NODE", "unavailable"),
        "numa_free_mb": pre.get("NUMA_FREE_MB", "unavailable"),
        "disk_avail_kb": pre.get("DISK_AVAIL_KB", "unavailable"),
        "disk_model": pre.get("DISK_MODEL", "unavailable"),
        "memory_total_gb": pre.get("MEM_TOTAL", "unavailable"),
        "memory_free_gb": pre.get("MEM_FREE", "unavailable"),
        "rocksdb_version": deploy.get("ROCKSDB_VERSION", rocks.get("ROCKSDB_VERSION", "unavailable")),
        "rocksdb_flags": deploy.get("ROCKSDB_FLAGS", rocks.get("ROCKSDB_FLAGS", "unavailable")),
        "ycsb_commit": deploy.get("YCSB_COMMIT", ycsb.get("YCSB_COMMIT", "unavailable")),
        "java": deps.get("JAVA_VERSION", deploy.get("JAVA_VERSION", "unavailable")),
        "gcc": deps.get("GCC_VERSION", deploy.get("GCC_VERSION", "unavailable")),
        "maven": deps.get("MAVEN_VERSION", deploy.get("MAVEN_VERSION", "unavailable")),
    }
    if collected:
        env["collected"] = collected
        env["java"] = deps.get("JAVA_VERSION") or collected["java"].splitlines()[0]
        env["gcc"] = deps.get("GCC_VERSION") or collected["gcc"].splitlines()[0]
        env["maven"] = deps.get("MAVEN_VERSION") or collected["mvn"].splitlines()[0]
    env["rocksdb_version"] = deploy.get("ROCKSDB_VERSION") or rocks.get("ROCKSDB_VERSION") or os.environ.get("ROCKSDB_VERSION") or "unavailable"
    env["ycsb_commit"] = deploy.get("YCSB_COMMIT") or ycsb.get("YCSB_COMMIT") or os.environ.get("YCSB_COMMIT") or "unavailable"
    return env


def latest_logs(result_dir):
    logs = {}
    for path in result_dir.glob("baseline_workload*.log"):
        match = WORKLOAD_RE.match(path.name)
        if not match:
            continue
        workload = match.group(1)
        if workload not in logs or path.stat().st_mtime > logs[workload].stat().st_mtime:
            logs[workload] = path
    return logs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--data-dir", default="/data/workload_data")
    parser.add_argument("--profile", default="baseline")
    parser.add_argument("--skip-env", action="store_true")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    env_dir = result_dir / "env"
    bench = read_env(env_dir / "bench.env")
    warm = read_env(env_dir / "warmup.env")
    load_env = read_env(env_dir / "load.env")
    post_drop = read_env(env_dir / "post_drop_memory.env")
    cleanup = read_env(env_dir / "cleanup.env")
    command_file = env_dir / "command.txt"
    command = command_file.read_text().strip() if command_file.exists() else "unavailable"

    load = parse_ycsb(result_dir / "load.log")
    workloads = {}
    missing = []
    for workload, path in sorted(latest_logs(result_dir).items()):
        metrics = parse_ycsb(path)
        workloads[workload] = metrics
        for key in ("throughput_ops_sec", "operations"):
            if key not in metrics:
                missing.append(f"workload {workload}: {key}")
        per_op = metrics.get("per_operation", {})
        if "average_latency_us" not in metrics and not any(
                "average_latency_us" in op for op in per_op.values()):
            missing.append(f"workload {workload}: average_latency_us")
        for percentile in ("p95_latency_us", "p99_latency_us"):
            if percentile not in metrics and not any(
                    percentile in op for op in per_op.values()):
                missing.append(f"workload {workload}: {percentile}")
        if metrics.get("return_errors", 0):
            missing.append(f"workload {workload}: return_errors={metrics['return_errors']}")
    if "throughput_ops_sec" not in load:
        missing.append("load: throughput_ops_sec")
    if load.get("return_errors", 0):
        missing.append(f"load: return_errors={load['return_errors']}")
    if warm.get("FINAL_STARTED") != "0":
        missing.append("warmup: FINAL_STARTED is not 0")
    if bench.get("FAILED_WORKLOAD"):
        missing.append(f"benchmark failed: {bench.get('FAILED_WORKLOAD')}")
    done = bench.get("WORKLOADS_DONE", "")
    if args.profile == "baseline":
        if done != BASELINE_WORKLOADS:
            missing.append(f"benchmark: WORKLOADS_DONE={done!r}, expected {BASELINE_WORKLOADS!r}")
        expected = set(BASELINE_WORKLOADS.split())
    else:
        expected = set(done.split()) if done else set()
    if set(workloads) != expected:
        missing.append(f"workload logs {sorted(workloads)} do not match {sorted(expected)}")
    if cleanup:
        backup_status = cleanup.get("BACKUP_STATUS", cleanup.get("CLEANUP_STATUS", "unavailable"))
        if backup_status not in ("backed_up", "skipped"):
            missing.append(f"cleanup: backup status is {backup_status}")

    env = environment(result_dir, args.data_dir, args.skip_env)
    preflight_txt = read_preflight_txt(result_dir)
    env["hugepages_total"] = preflight_txt.get("hugepages_total", "unavailable")
    env["hugepages_free"] = preflight_txt.get("hugepages_free", "unavailable")
    env["hugepagesize"] = preflight_txt.get("hugepagesize", "unavailable")
    env["transparent_hugepages"] = preflight_txt.get("thp", "unavailable")
    env["filesystem"], env["mount_options"], env["rota"] = parse_disk_collected(env)
    rounds_path = result_dir / "warmup_rounds.txt"
    warmup_rounds_detail = []
    if rounds_path.exists():
        warmup_rounds_detail = [
            line.strip() for line in rounds_path.read_text(errors="replace").splitlines()
            if line.strip()
        ]
    env["pagecache_drop_at"] = post_drop.get("DROP_CACHES_AT", "unavailable")
    env["post_drop_numa_free_mb"] = post_drop.get("NUMA_FREE_MB", "unavailable")
    data_size = cleanup.get("DATA_BYTES", "unavailable")
    if data_size == "unavailable":
        try:
            data_path = Path(args.data_dir)
            if data_path.exists():
                data_size = str(sum(p.stat().st_size for p in data_path.rglob("*") if p.is_file()))
        except Exception:
            pass
    env["data_bytes"] = data_size

    params = {
        "record_count": load_env.get("RECORD_COUNT", "unavailable"),
        "operation_count": load_env.get("OPERATION_COUNT", "unavailable"),
        "field_length": load_env.get("FIELD_LENGTH", "default"),
        "field_count": load_env.get("FIELD_COUNT", "default"),
        "load_threads": load_env.get("LOAD_THREADS", "unavailable"),
        "warmup_operation_count": warm.get("WARMUP_OPERATION_COUNT", "unavailable"),
        "bench_threads": bench.get("BENCH_THREADS", "unavailable"),
        "workloads": done or "unavailable",
        "cpu_list": env.get("cpu_list", "unavailable"),
        "numa_node": env.get("numa_node", "unavailable"),
    }
    config = {}
    ycsb_env = read_env(env_dir / "ycsb.env")
    deploy_env = read_env(env_dir / "deploy.env")
    option_path = deploy_env.get("YCSB_OPTIONS", ycsb_env.get("YCSB_OPTIONS", ""))
    if option_path and Path(option_path).exists():
        digest = hashlib.sha256(Path(option_path).read_bytes()).hexdigest()
        config["YCSB_OPTIONS"] = {"path": option_path, "sha256": digest}
    else:
        config["YCSB_OPTIONS"] = {"path": option_path or "unavailable", "sha256": "unavailable"}

    success = not missing
    report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = socket.gethostname()
    summary = {
        "profile": args.profile,
        "success": success,
        "report_time": report_time,
        "hostname": hostname,
        "result_dir": str(result_dir),
        "missing": missing,
        "environment": env,
        "parameters": params,
        "config": config,
        "command": command,
        "load": load,
        "warmup": {
            "rounds": warm.get("ROUNDS", "unavailable"),
            "rounds_detail": warmup_rounds_detail,
            "final_started": warm.get("FINAL_STARTED", "unavailable"),
            "final_finished": warm.get("FINAL_FINISHED", "unavailable"),
        },
        "workloads": workloads,
        "cleanup": cleanup,
    }

    (result_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    with (result_dir / "summary.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "workload", "throughput_ops_sec", "average_latency_us",
            "p95_latency_us", "p99_latency_us", "operations",
        ])
        for workload in sorted(workloads):
            values = workloads[workload]
            writer.writerow([
                workload,
                values.get("throughput_ops_sec", ""),
                values.get("average_latency_us", ""),
                values.get("p95_latency_us", ""),
                values.get("p99_latency_us", ""),
                values.get("operations", ""),
            ])

    lines = [
        "# RocksDB Baseline Report",
        "",
        f"- report_time: `{report_time}`",
        f"- hostname: `{hostname}`",
        f"- profile: `{args.profile}`",
        f"- result_dir: `{result_dir}`",
        f"- success: `{success}`",
        "",
        "## Environment",
        "",
        "| item | value |",
        "|---|---|",
    ]
    labels = {
        "arch": "Architecture", "os": "OS", "kernel": "Kernel",
        "cpu_model": "CPU model", "cpu_list": "CPU list", "numa_node": "NUMA node",
        "rocksdb_version": "RocksDB version", "rocksdb_flags": "RocksDB flags",
        "ycsb_commit": "YCSB commit", "java": "Java", "gcc": "GCC", "maven": "Maven",
    }
    for key in ("arch", "os", "kernel", "cpu_model", "cpu_list", "numa_node",
                "rocksdb_version", "rocksdb_flags", "ycsb_commit", "java", "gcc", "maven"):
        lines.append(f"| {labels[key]} | {env.get(key, 'unavailable')} |")
    lines += [
        "",
        "## Memory",
        "",
        f"- numa_free_mb: `{env.get('numa_free_mb')}`",
        f"- memory_total_gb: `{env.get('memory_total_gb')}`",
        f"- memory_free_gb: `{env.get('memory_free_gb')}`",
        f"- hugepages_total: `{env.get('hugepages_total')}`",
        f"- hugepages_free: `{env.get('hugepages_free')}`",
        f"- hugepagesize: `{env.get('hugepagesize')}`",
        f"- transparent_hugepages: `{env.get('transparent_hugepages')}`",
        "",
        "## Disk",
        "",
        f"- disk_model: `{env.get('disk_model')}`",
        f"- filesystem: `{env.get('filesystem')}`",
        f"- mount_options: `{env.get('mount_options')}`",
        f"- rota: `{env.get('rota')}`",
        f"- disk_avail_kb: `{env.get('disk_avail_kb')}`",
        f"- data_bytes: `{env.get('data_bytes')}`",
        "",
        "## Warmup",
        "",
        f"- warmup_rounds: `{summary['warmup']['rounds']}`",
        f"- final_started: `{summary['warmup']['final_started']}`",
        f"- final_finished: `{summary['warmup']['final_finished']}`",
        f"- warmup_operation_count: `{params['warmup_operation_count']}`",
    ]
    if warmup_rounds_detail:
        lines.append("- rounds_detail:")
        for round_line in warmup_rounds_detail:
            lines.append(f"  - {round_line}")
    lines += [
        "",
        "## Parameters",
        "",
        f"- record_count: `{params['record_count']}`",
        f"- operation_count: `{params['operation_count']}`",
        f"- field_length: `{params['field_length']}`",
        f"- load_threads: `{params['load_threads']}`",
        f"- bench_threads: `{params['bench_threads']}`",
        f"- workloads: `{params['workloads']}`",
        "",
        "## Pagecache",
        "",
        f"- dropped_at: `{env.get('pagecache_drop_at')}`",
        f"- post_drop_numa_free_mb: `{env.get('post_drop_numa_free_mb')}`",
        "",
        "## Data Backup",
        "",
        f"- status: `{cleanup.get('BACKUP_STATUS', cleanup.get('CLEANUP_STATUS', 'unavailable'))}`",
        f"- backup_dir: `{cleanup.get('BACKUP_DIR', 'unavailable')}`",
        f"- data_bytes: `{cleanup.get('DATA_BYTES', 'unavailable')}`",
    ]
    if cleanup.get("BACKUP_EXIT_CODE"):
        lines.append(f"- backup_exit_code: `{cleanup['BACKUP_EXIT_CODE']}`")
    lines += [
        "",
        "## Command",
        "",
        f"`{command}`",
        "",
        "## Workloads",
        "",
        "| workload | throughput(ops/s) | avg(us) | p95(us) | p99(us) | ops | return_errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for workload in sorted(workloads):
        values = workloads[workload]
        lines.append(
            f"| {workload} | {values.get('throughput_ops_sec', '')} | "
            f"{values.get('average_latency_us', '')} | {values.get('p95_latency_us', '')} | "
            f"{values.get('p99_latency_us', '')} | {values.get('operations', '')} | "
            f"{values.get('return_errors', '')} |"
        )
    lines += [
        "",
        "## Per-operation latency",
        "",
        "| workload | operation | operations | avg(us) | p95(us) | p99(us) |",
        "|---|---|---|---|---|---|",
    ]
    for workload in sorted(workloads):
        for operation, metrics in sorted(workloads[workload].get("per_operation", {}).items()):
            lines.append(
                f"| {workload} | {operation} | {metrics.get('operations', '')} | "
                f"{metrics.get('average_latency_us', '')} | {metrics.get('p95_latency_us', '')} | "
                f"{metrics.get('p99_latency_us', '')} |"
            )
    if missing:
        lines += ["", "## Missing", ""] + [f"- {item}" for item in missing]
    (result_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
