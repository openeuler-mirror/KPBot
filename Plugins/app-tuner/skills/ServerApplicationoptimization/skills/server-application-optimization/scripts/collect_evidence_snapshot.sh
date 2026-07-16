#!/usr/bin/env bash
set -euo pipefail

# collect_evidence_snapshot.sh
# 在压测运行期间并发采集所有 subagent 需要的动态数据，压测结束后采集静态数据。
# 用法:
#   ./collect_evidence_snapshot.sh \
#     --target-pid 1234 --cpu-set 64-71 --iface enp133s0 \
#     --mysql-port 3308 --duration 60 --output-dir evidence/ \
#     --current-run-id mysql-20260621T100000 --current-run-started-at 2026-06-21T10:00:00+08:00

usage() {
    echo "Usage: $0 --target-pid PID --cpu-set CPUS --iface IFACE [--mysql-port PORT] [--duration SECONDS] --output-dir DIR --current-run-id ID --current-run-started-at ISO_TIME [--target-identity-path PATH]"
    exit 1
}

TARGET_PID=""
CPU_SET=""
IFACE=""
MYSQL_PORT=""
DURATION="60"
OUTPUT_DIR=""
CURRENT_RUN_ID=""
CURRENT_RUN_STARTED_AT=""
TARGET_IDENTITY_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-pid) TARGET_PID="$2"; shift 2 ;;
        --cpu-set) CPU_SET="$2"; shift 2 ;;
        --iface) IFACE="$2"; shift 2 ;;
        --mysql-port) MYSQL_PORT="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --current-run-id) CURRENT_RUN_ID="$2"; shift 2 ;;
        --current-run-started-at) CURRENT_RUN_STARTED_AT="$2"; shift 2 ;;
        --target-identity-path) TARGET_IDENTITY_PATH="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

[[ -z "${TARGET_PID}" ]] && { echo "ERROR: --target-pid is required"; usage; }
[[ -z "${CPU_SET}" ]] && { echo "ERROR: --cpu-set is required"; usage; }
[[ -z "${IFACE}" ]] && { echo "ERROR: --iface is required"; usage; }
[[ -z "${OUTPUT_DIR}" ]] && { echo "ERROR: --output-dir is required"; usage; }
[[ -z "${CURRENT_RUN_ID}" ]] && { echo "ERROR: --current-run-id is required"; usage; }
[[ -z "${CURRENT_RUN_STARTED_AT}" ]] && { echo "ERROR: --current-run-started-at is required"; usage; }
if [[ -n "${TARGET_IDENTITY_PATH}" && ! -f "${TARGET_IDENTITY_PATH}" ]]; then
    echo "ERROR: --target-identity-path does not exist: ${TARGET_IDENTITY_PATH}" >&2
    exit 1
fi

RUNNING_DIR="${OUTPUT_DIR}/workload_running"
STATIC_DIR="${OUTPUT_DIR}/static"
mkdir -p "${RUNNING_DIR}" "${STATIC_DIR}"

echo "=== Evidence Snapshot Collection ==="
echo "Target PID: ${TARGET_PID}"
echo "CPU set: ${CPU_SET}"
echo "Interface: ${IFACE}"
echo "Duration: ${DURATION}s"
echo "Output: ${OUTPUT_DIR}"
echo "Current run ID: ${CURRENT_RUN_ID}"
echo "Current run started at: ${CURRENT_RUN_STARTED_AT}"
echo ""

# --- 动态数据采集（需要在压测运行期间并发执行）---
# 调用方应在启动压测后调用本脚本的 --collect-dynamic 阶段

echo "--- Phase 1: Dynamic data collection (${DURATION}s) ---"

# perf sampling
perf record -F 99 -g -p "${TARGET_PID}" -o "${RUNNING_DIR}/perf.data" -- sleep "${DURATION}" &
PERF_PID=$!

# pidstat threads CPU
pidstat -p "${TARGET_PID}" -t -u 1 "${DURATION}" > "${RUNNING_DIR}/pidstat_threads.txt" 2>&1 &
PIDSTAT_CPU_PID=$!

# pidstat context switches
pidstat -p "${TARGET_PID}" -t -w 1 "${DURATION}" > "${RUNNING_DIR}/pidstat_ctxswitch.txt" 2>&1 &
PIDSTAT_CTX_PID=$!

# mpstat
mpstat -P "${CPU_SET}" 1 "${DURATION}" > "${RUNNING_DIR}/mpstat.txt" 2>&1 &
MPSTAT_PID=$!

# sar network
sar -n DEV 1 "${DURATION}" > "${RUNNING_DIR}/sar_network.txt" 2>&1 &
SAR_PID=$!

# topdown/cache counters. Event names vary across CPU vendors, so failures are recorded as degraded evidence.
perf stat -p "${TARGET_PID}" \
    -e cycles,instructions,L1-icache-load-misses,L1-icache-loads,LLC-load-misses,LLC-loads,context-switches \
    -o "${RUNNING_DIR}/perf_stat_cache.txt" -- sleep "${DURATION}" 2>"${RUNNING_DIR}/perf_stat_cache.err" &
PERF_STAT_CACHE_PID=$!

perf stat -M TopdownL1 -p "${TARGET_PID}" \
    -o "${RUNNING_DIR}/perf_stat_topdown_l1.txt" -- sleep "${DURATION}" 2>"${RUNNING_DIR}/perf_stat_topdown_l1.err" &
PERF_STAT_TOPDOWN_PID=$!

# Instantaneous snapshots (mid-collection)
sleep 5
ps -L -p "${TARGET_PID}" -o pid,tid,psr,pcpu,comm > "${RUNNING_DIR}/thread_dist.txt" 2>&1 || true

# IRQ affinity
(for irq in /proc/irq/*/smp_affinity_list; do echo "$(basename "$(dirname "$irq")"): $(cat "$irq" 2>/dev/null)"; done) > "${RUNNING_DIR}/irq_affinity.txt" 2>&1 || true

# ethtool stats
ethtool -S "${IFACE}" > "${RUNNING_DIR}/ethtool_stats.txt" 2>&1 || true

# process maps
cat "/proc/${TARGET_PID}/maps" > "${RUNNING_DIR}/process_maps.txt" 2>&1 || true

# MySQL status (if port provided)
if [[ -n "${MYSQL_PORT}" ]]; then
    mysql -h 127.0.0.1 -P "${MYSQL_PORT}" -u root -e "SHOW ENGINE INNODB STATUS\G" > "${RUNNING_DIR}/innodb_status.txt" 2>&1 || echo "MySQL InnoDB status collection skipped" > "${RUNNING_DIR}/innodb_status.txt"
    mysql -h 127.0.0.1 -P "${MYSQL_PORT}" -u root -e "SHOW GLOBAL STATUS" > "${RUNNING_DIR}/global_status.txt" 2>&1 || echo "MySQL global status collection skipped" > "${RUNNING_DIR}/global_status.txt"
fi

# Wait for all background collectors
echo "Waiting for dynamic data collection to complete..."
wait "${PERF_PID}" 2>/dev/null || true
wait "${PIDSTAT_CPU_PID}" 2>/dev/null || true
wait "${PIDSTAT_CTX_PID}" 2>/dev/null || true
wait "${MPSTAT_PID}" 2>/dev/null || true
wait "${SAR_PID}" 2>/dev/null || true
wait "${PERF_STAT_CACHE_PID}" 2>/dev/null || true
wait "${PERF_STAT_TOPDOWN_PID}" 2>/dev/null || true

if [[ -s "${RUNNING_DIR}/perf.data" ]]; then
    perf report --stdio -i "${RUNNING_DIR}/perf.data" --sort comm,dso,symbol --percent-limit 0.2 > "${RUNNING_DIR}/perf_report_by_process_dso_symbol.txt" 2>&1 || true
    perf report --stdio -i "${RUNNING_DIR}/perf.data" --sort dso,symbol --percent-limit 0.2 > "${RUNNING_DIR}/perf_report_by_dso_symbol.txt" 2>&1 || true
    perf script -i "${RUNNING_DIR}/perf.data" > "${RUNNING_DIR}/perf_script.txt" 2>&1 || true
    if command -v stackcollapse-perf.pl >/dev/null 2>&1; then
        stackcollapse-perf.pl "${RUNNING_DIR}/perf_script.txt" > "${RUNNING_DIR}/flamegraph.folded" 2>&1 || true
    fi
fi

echo "--- Phase 1 complete ---"

# --- 静态数据采集（压测结束后）---
echo "--- Phase 2: Static data collection ---"

lscpu > "${STATIC_DIR}/lscpu.txt" 2>&1 || true
sysctl -a > "${STATIC_DIR}/sysctl.txt" 2>&1 || true
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null > "${STATIC_DIR}/governor.txt" || true
ulimit -a > "${STATIC_DIR}/ulimit.txt" 2>&1 || true

# Binary analysis
BINARY=""
if [[ -e "/proc/${TARGET_PID}/exe" ]]; then
    BINARY="$(readlink -f "/proc/${TARGET_PID}/exe")"
fi

if [[ -n "${BINARY}" && -x "${BINARY}" ]]; then
    readelf -A "${BINARY}" > "${STATIC_DIR}/readelf_arch.txt" 2>&1 || true
    readelf -d "${BINARY}" > "${STATIC_DIR}/readelf_deps.txt" 2>&1 || true
    ldd "${BINARY}" > "${STATIC_DIR}/ldd.txt" 2>&1 || true
else
    echo "Binary not found or not executable" > "${STATIC_DIR}/readelf_arch.txt"
fi

# GCC target flags
gcc -mcpu=native -Q --help=target > "${STATIC_DIR}/gcc_target.txt" 2>&1 || echo "gcc not available" > "${STATIC_DIR}/gcc_target.txt"

# Network config
ethtool -c "${IFACE}" > "${STATIC_DIR}/ethtool_coalesce.txt" 2>&1 || true
ethtool -g "${IFACE}" > "${STATIC_DIR}/ethtool_ring.txt" 2>&1 || true
for q in /sys/class/net/"${IFACE}"/queues/rx-*/rps_cpus; do echo "$(basename "$(dirname "$q")"): $(cat "$q" 2>/dev/null)"; done > "${STATIC_DIR}/rps_cpus.txt" 2>&1 || true

# MySQL variables (if port provided)
if [[ -n "${MYSQL_PORT}" ]]; then
    mysql -h 127.0.0.1 -P "${MYSQL_PORT}" -u root -e "SHOW VARIABLES" > "${STATIC_DIR}/mysql_variables.txt" 2>&1 || echo "MySQL variables collection skipped" > "${STATIC_DIR}/mysql_variables.txt"
fi

echo "--- Phase 2 complete ---"

# --- 生成 snapshot_metadata.json ---
SNAPSHOT_TIME="$(date -Iseconds)"
python3 - "${OUTPUT_DIR}/snapshot_metadata.json" "${RUNNING_DIR}" "${STATIC_DIR}" \
    "${SNAPSHOT_TIME}" "${TARGET_PID}" "${CPU_SET}" "${IFACE}" "${MYSQL_PORT}" "${DURATION}" \
    "${CURRENT_RUN_ID}" "${CURRENT_RUN_STARTED_AT}" "${TARGET_IDENTITY_PATH}" <<'PY'
import json
import os
import sys

(
    metadata_path,
    running_dir,
    static_dir,
    snapshot_time,
    target_pid,
    cpu_set,
    iface,
    mysql_port,
    duration,
    current_run_id,
    current_run_started_at,
    target_identity_path,
) = sys.argv[1:]

def list_files(path):
    try:
        return sorted(
            entry.name
            for entry in os.scandir(path)
            if entry.is_file()
        )
    except FileNotFoundError:
        return []

def load_json(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "unreadable", "error": str(exc), "path": path}

payload = {
    "current_run_id": current_run_id,
    "current_run_started_at": current_run_started_at,
    "current_evidence_status": "current",
    "evidence_freshness_policy": "current_run_id, target identity, and collection time must match before dynamic routing",
    "snapshot_time": snapshot_time,
    "target_pid": int(target_pid),
    "cpu_set": cpu_set,
    "iface": iface,
    "mysql_port": mysql_port or None,
    "collection_duration": int(duration),
    "target_identity_path": target_identity_path or None,
    "target_identity": load_json(target_identity_path),
    "dynamic_files": list_files(running_dir),
    "static_files": list_files(static_dir),
}

with open(metadata_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

# --- 生成 performance_signal_summary.json ---
python3 - "${OUTPUT_DIR}/performance_signal_summary.json" "${RUNNING_DIR}" "${DURATION}" <<'PY'
import json
import os
import re
import sys
from collections import defaultdict

summary_path, running_dir, duration = sys.argv[1:]
duration = max(float(duration or 1), 1.0)

network_tokens = (
    "tcp", "udp", "socket", "sock", "epoll", "poll", "select", "sendmsg", "recvmsg",
    "sendto", "recvfrom", "softirq", "net_rx", "net_tx", "napi", "skb", "xmit",
    "gro", "gso", "iptables", "iptable", "nft_", "netfilter", "mlx", "ixgbe", "i40e",
    "ena_", "virtio_net", "irq",
)

def read_text(name):
    path = os.path.join(running_dir, name)
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""

def parse_perf_report(text):
    functions = []
    dso_totals = defaultdict(float)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)%\s+(.+)$", line)
        if not match:
            continue
        percent = float(match.group(1))
        rest = match.group(2)
        parts = re.split(r"\s{2,}", rest)
        if len(parts) >= 3:
            comm = parts[0]
            dso = parts[-2]
            symbol = parts[-1]
        elif len(parts) == 2:
            comm = ""
            dso, symbol = parts
        else:
            comm = ""
            dso = "unknown"
            symbol = rest
        lower_symbol = f"{dso} {symbol}".lower()
        category = "network" if any(token in lower_symbol for token in network_tokens) else "unknown"
        classification = "third_party" if ".so" in dso and not any(
            token in dso.lower() for token in ("linux-vdso", "ld-linux", "libc.so", "libpthread", "libm.so", "librt.so")
        ) else "system_or_application"
        functions.append({
            "percent": percent,
            "process": comm,
            "dso": dso,
            "symbol": symbol,
            "category": category,
            "classification": classification,
        })
        dso_totals[dso] += percent
    dso_rank = [
        {
            "dso": dso,
            "percent": round(percent, 4),
            "classification": "third_party" if ".so" in dso and not any(
                token in dso.lower() for token in ("linux-vdso", "ld-linux", "libc.so", "libpthread", "libm.so", "librt.so")
            ) else "system_or_application",
        }
        for dso, percent in sorted(dso_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    return functions[:100], dso_rank[:50]

def parse_counter(text, event_name):
    pattern = re.compile(r"^\s*([0-9][0-9,\.]*)\s+" + re.escape(event_name) + r"\b", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return 0.0
    return float(match.group(1).replace(",", ""))

def pct(numerator, denominator):
    if denominator <= 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 4)

perf_report = read_text("perf_report_by_process_dso_symbol.txt") or read_text("perf_report_by_dso_symbol.txt")
hotspot_functions, hotspot_dsos = parse_perf_report(perf_report)

cache_text = read_text("perf_stat_cache.txt") + "\n" + read_text("perf_stat_cache.err")
l1_misses = parse_counter(cache_text, "L1-icache-load-misses")
l1_loads = parse_counter(cache_text, "L1-icache-loads")
llc_misses = parse_counter(cache_text, "LLC-load-misses")
llc_loads = parse_counter(cache_text, "LLC-loads")
context_switches = parse_counter(cache_text, "context-switches")

l1_icache_miss_pct = pct(l1_misses, l1_loads)
llc_miss_pct = pct(llc_misses, llc_loads)
context_switch_rate = round(context_switches / duration, 4) if context_switches else 0.0

network_hotspot_pct = round(sum(item["percent"] for item in hotspot_functions if item.get("category") == "network"), 4)
third_party_hotspot_pct = round(sum(item["percent"] for item in hotspot_dsos if item.get("classification") == "third_party"), 4)

payload = {
    "schema_version": "1.0",
    "summary_source": "collect_evidence_snapshot.sh",
    "hotspot_function_rank": hotspot_functions,
    "hotspot_dso_rank": hotspot_dsos,
    "topdown": {
        "l1_icache_miss_pct": l1_icache_miss_pct,
        "l1_icache_miss_high": l1_icache_miss_pct >= 5.0,
        "l3_cache_miss_pct": llc_miss_pct,
        "l3_cache_miss_high": llc_miss_pct >= 5.0,
    },
    "threading": {
        "context_switches": context_switches,
        "context_switch_rate_per_sec": context_switch_rate,
        "context_switch_high": context_switch_rate >= 1000.0,
    },
    "detected_signals": {
        "third_party_library_hotspot": third_party_hotspot_pct >= 5.0,
        "network_hotspot_high": network_hotspot_pct >= 3.0,
        "l1_icache_miss_high": l1_icache_miss_pct >= 5.0,
        "l3_cache_miss_high": llc_miss_pct >= 5.0,
        "context_switch_high": context_switch_rate >= 1000.0,
    },
    "degraded_capabilities": [],
}

if not perf_report:
    payload["degraded_capabilities"].append("perf_report_unavailable")
if not cache_text.strip():
    payload["degraded_capabilities"].append("perf_stat_cache_unavailable")

with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

echo ""
echo "=== Evidence snapshot complete ==="
echo "Dynamic data: ${RUNNING_DIR}/"
echo "Static data:  ${STATIC_DIR}/"
echo "Metadata:     ${OUTPUT_DIR}/snapshot_metadata.json"
echo "Performance summary: ${OUTPUT_DIR}/performance_signal_summary.json"
