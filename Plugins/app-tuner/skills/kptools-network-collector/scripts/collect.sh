#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PID=""
IFACE=""
DURATION=5
MODE=""
HELP=false

print_help() {
    cat <<'EOF'
kptools-network-collector — Collect network and interrupt performance data (read-only)

Usage:
  collect.sh --<mode> [-i <IFACE>] [-p <PID>] [-d <sec>]

Modes:
  --iface        Interface discovery: UP links + carrier + NUMA node summary
  --bandwidth    Per-interface throughput over time (sar -n DEV 1 D)
  --drop         Packet drop/error delta via /proc/net/dev double sampling (+ netstat -i / ip -s link)
  --retrans      TCP retransmission counters (netstat -s, fallback nstat)
  --sock         TCP socket queues (ss -tinp); -p <PID> filters and aggregates Recv-Q/Send-Q
  --nic        NIC parameters (ethtool -l/-c/-g/-k/-S/-i) for -i <IFACE>;
                default: UP physical NICs only (virtual veth/bridge skipped, use -i to target)
  --rss          RSS/RPS/XPS config (queues/*/rps_cpus, xps_cpus, rps_flow_cnt)
  --irq          NIC IRQ lines from /proc/interrupts + per-IRQ affinity lists
  --irqdelta     Per-IRQ counter delta over -d seconds + max/min imbalance ratio
  --numa         NIC NUMA topology (numa_node, local_cpulist, msi_irqs)
  --softirq      /proc/softirqs snapshot
  --all          Run all modes in order

Options:
  -i <IFACE>     Target interface (optional; default: all UP interfaces)
  -p <PID>       Target PID for --sock filter
  -d <sec>       Duration for timed modes (default: 5)
  --help         Show this help

stdout: raw collected data; stderr: diagnostics; exit 0/non-zero
Read-only: no ethtool -L/-C/-G/-K, no sysfs writes, no IRQ rebinding.
EOF
}

# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -i|-p|-d) echo "ERROR: option ${!#} requires a value" >&2; exit 1 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -i) IFACE="$2"; shift 2 ;;
        -p) PID="$2"; shift 2 ;;
        -d) DURATION="$2"; shift 2 ;;
        --iface|--bandwidth|--drop|--retrans|--sock|--nic|--rss|--irq|--irqdelta|--numa|--softirq|--all) MODE="$1"; shift ;;
        --help) HELP=true; shift ;;
        *) echo "ERROR: unknown option: $1" >&2; print_help >&2; exit 1 ;;
    esac
done

if [ "$HELP" = true ]; then
    print_help
    exit 0
fi

if [ -z "$MODE" ]; then
    echo "ERROR: a --<mode> is required (see --help)" >&2
    exit 1
fi

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 1 ]; then
    echo "ERROR: -d must be a positive integer (got '$DURATION')" >&2
    exit 1
fi

require_tool() {
    local tool="$1" pkg="$2"
    if ! command -v "$tool" &>/dev/null; then
        echo "ERROR: $tool not found (install: $pkg)" >&2
        exit 1
    fi
}

up_ifaces() {
    ip -o link show 2>/dev/null | awk -F': ' '/state UP/ {print $2}' | cut -d@ -f1
}

resolve_ifaces() {
    if [ -n "$IFACE" ]; then
        if ! ip link show "$IFACE" &>/dev/null; then
            echo "ERROR: interface $IFACE does not exist" >&2
            exit 1
        fi
        echo "$IFACE"
    else
        up_ifaces
    fi
}

mode_iface() {
    require_tool ip "yum install iproute2 or apt install iproute2"
    echo "# UP interfaces: carrier / NUMA node"
    printf "%-16s %-8s %-6s %s\n" "IFACE" "CARRIER" "NUMA" "SPEED"
    local ifc
    for ifc in $(up_ifaces); do
        local carrier numa speed
        carrier=$(cat "/sys/class/net/$ifc/carrier" 2>/dev/null || echo "n/a")
        numa=$(cat "/sys/class/net/$ifc/device/numa_node" 2>/dev/null || echo "unknown")
        speed=$(cat "/sys/class/net/$ifc/speed" 2>/dev/null || echo "n/a")
        printf "%-16s %-8s %-6s %s\n" "$ifc" "$carrier" "$numa" "$speed"
    done
}

mode_bandwidth() {
    require_tool sar "yum install sysstat or apt install sysstat"
    echo "# sar -n DEV 1 $DURATION"
    sar -n DEV 1 "$DURATION"
}

mode_drop() {
    local sample
    sample() {
        awk 'NR>2 && $1!~/^lo:/ {rx_bytes+=$2; rx_drop+=$5; tx_bytes+=$10; tx_drop+=$13} END {print rx_bytes+0, rx_drop+0, tx_bytes+0, tx_drop+0}' /proc/net/dev
    }
    local before after
    before=$(sample)
    sleep "$DURATION"
    after=$(sample)
    echo "# /proc/net/dev double sample over ${DURATION}s (lo excluded)"
    # sample 输出 4 字段（rx_bytes rx_drop tx_bytes tx_drop），before=$1..$4，after=$5..$8
    echo "$before $after" | awk -v dur="$DURATION" '{
        rx_rate=($5-$1)/dur; rx_drop=$6-$2; tx_rate=($7-$3)/dur; tx_drop=$8-$4;
        printf "rx_bytes_per_sec=%.0f rx_drop_delta=%.0f tx_bytes_per_sec=%.0f tx_drop_delta=%.0f\n", rx_rate, rx_drop, tx_rate, tx_drop;
        status="none";
        if (rx_drop+tx_drop > 0) status="drop_detected";
        printf "drop_status=%s\n", status;
    }'
    if command -v netstat &>/dev/null; then
        echo "# netstat -i (errors/drops zero-tolerance view)"
        netstat -i 2>/dev/null | grep -vE "^Kernel|^Iface" || true
    elif command -v ip &>/dev/null; then
        echo "# netstat not found, fallback: ip -s link"
        ip -s link 2>/dev/null || true
    fi
}

mode_retrans() {
    if command -v netstat &>/dev/null; then
        echo "# netstat -s (retransmission related)"
        netstat -s 2>/dev/null | grep -iE "retransmit|listen drop|listen overflow|backlog" || echo "no retransmission counters found"
    elif command -v nstat &>/dev/null; then
        echo "# netstat not found, fallback: nstat"
        nstat -az 2>/dev/null | grep -E "TcpRetransSegs|TcpOutSegs|TcpExtListenDrops|TcpExtListenOverflows|TcpExtTCPBacklogDrop" || echo "no retransmission counters found"
    else
        echo "ERROR: netstat and nstat both not found (install: net-tools or iproute2)" >&2
        exit 1
    fi
}

mode_sock() {
    require_tool ss "yum install iproute2 or apt install iproute2"
    if [ -n "$PID" ]; then
        echo "# ss -tinp filtered by pid=$PID, aggregated Recv-Q/Send-Q"
        ss -tinp 2>/dev/null | awk -v pid="$PID" '
            /^State/ {next}
            $0 ~ ("pid=" pid ",") {
                recvq += $2; sendq += $3; sockets++;
                # retrans:X/Y — X=在途重传段(tcpi_retrans, 瞬时值)，Y=连接累计重传段(tcpi_total_retrans)
                # 聚合取 Y（累计计数器），跨 socket 求和为"自连接建立以来的重传段总数"
                if (match($0, /retrans:[0-9]+\/([0-9]+)/)) {
                    split(substr($0, RSTART, RLENGTH), seg, /[:\/]/);
                    retrans += seg[3];
                }
            }
            END {
                printf "socket_count=%d total_recvq=%d total_sendq=%d retrans_segments=%d\n", sockets, recvq+0, sendq+0, retrans+0;
                hint=(sockets>0)?"socket_backlog_sampled":"no_sockets_for_pid";
                printf "hint=%s\n", hint;
            }'
        echo "# raw ss -tinp for pid=$PID"
        ss -tinp 2>/dev/null | awk -v pid="$PID" '/^State/{print; next} $0 ~ ("pid=" pid ",") {print}'
    else
        echo "# ss -tinp (all TCP sockets)"
        ss -tinp 2>/dev/null || true
    fi
}

mode_nic() {
    require_tool ethtool "yum install ethtool or apt install ethtool"
    local targets
    targets=$(resolve_ifaces) || return 1
    local ifc skipped=""
    for ifc in $targets; do
        # 未指定 -i 时跳过无 PCI 设备的虚拟接口（veth/bridge 等）：
        # 容器宿主机上数百 veth × 6 次 ethtool 不可控；显式 -i 仍可指定任意接口
        if [ -z "$IFACE" ] && [ ! -e "/sys/class/net/$ifc/device" ]; then
            skipped="$skipped $ifc"
            continue
        fi
        echo "========== $ifc =========="
        local opt
        for opt in -l -c -g -k -S -i; do
            echo "# ethtool $opt $ifc"
            timeout 10 ethtool "$opt" "$ifc" 2>&1 || echo "ethtool $opt failed (unsupported?)"
        done
    done
    if [ -n "$skipped" ]; then
        echo "# skipped virtual interfaces without PCI device (use -i <IFACE> to target them):$skipped" >&2
    fi
}

mode_rss() {
    local targets
    targets=$(resolve_ifaces) || return 1
    local ifc q
    for ifc in $targets; do
        echo "========== $ifc =========="
        for q in "/sys/class/net/$ifc"/queues/rx-*/rps_cpus; do
            [ -e "$q" ] || continue
            echo "$(basename "$(dirname "$q")")/rps_cpus: $(cat "$q" 2>/dev/null || echo unreadable)"
        done
        for q in "/sys/class/net/$ifc"/queues/tx-*/xps_cpus; do
            [ -e "$q" ] || continue
            echo "$(basename "$(dirname "$q")")/xps_cpus: $(cat "$q" 2>/dev/null || echo unreadable)"
        done
        for q in "/sys/class/net/$ifc"/queues/rx-*/rps_flow_cnt; do
            [ -e "$q" ] || continue
            echo "$(basename "$(dirname "$q")")/rps_flow_cnt: $(cat "$q" 2>/dev/null || echo unreadable)"
        done
    done
}

resolve_irq_pattern() {
    local ifc="$1"
    if [ -n "$ifc" ]; then
        if grep -qE "$ifc" /proc/interrupts 2>/dev/null; then
            echo "$ifc"
            return
        fi
        local drv
        drv=$(basename "$(readlink -f "/sys/class/net/$ifc/device/driver" 2>/dev/null)" 2>/dev/null || true)
        if [ -n "$drv" ] && [ "$drv" != "null" ] && grep -qE "$drv" /proc/interrupts 2>/dev/null; then
            echo "$drv"
            return
        fi
    fi
    echo "eth|enp|ens|eno|bond|virtio|mlx5"
}

mode_irq() {
    local pattern
    pattern=$(resolve_irq_pattern "$IFACE")
    echo "# NIC IRQ lines (pattern: $pattern)"
    if [ ! -r /proc/interrupts ]; then
        echo "ERROR: /proc/interrupts not readable" >&2
        exit 1
    fi
    grep -E "$pattern" /proc/interrupts || echo "no matching IRQ lines"
    echo "# per-IRQ affinity"
    local irq
    for irq in $(grep -E "$pattern" /proc/interrupts 2>/dev/null | awk -F: '{print $1}' | tr -d ' '); do
        local aff eff
        aff=$(cat "/proc/irq/$irq/smp_affinity_list" 2>/dev/null || echo "unknown")
        eff=$(cat "/proc/irq/$irq/effective_affinity_list" 2>/dev/null || echo "unknown")
        echo "irq $irq: smp_affinity_list=$aff effective_affinity_list=$eff"
    done
}

mode_irqdelta() {
    if [ ! -r /proc/interrupts ]; then
        echo "ERROR: /proc/interrupts not readable" >&2
        exit 1
    fi
    require_tool python3 "yum install python3 or apt install python3"
    local pattern
    pattern=$(resolve_irq_pattern "$IFACE")
    # 原样保留 /proc/interrupts 行（含冒号）——下游 python 按 "irq: counts" 解析，
    # 中途剥掉冒号会导致所有行被丢弃、恒报 active_irqs=0
    snap() { grep -E "$pattern" /proc/interrupts 2>/dev/null || true; }
    IRQ_SNAP_BEFORE=$(mktemp /tmp/netcollect-irq.XXXXXX) || { echo "ERROR: mktemp failed" >&2; exit 1; }
    IRQ_SNAP_AFTER=$(mktemp /tmp/netcollect-irq.XXXXXX) || { echo "ERROR: mktemp failed" >&2; rm -f "$IRQ_SNAP_BEFORE"; exit 1; }
    trap 'rm -f "$IRQ_SNAP_BEFORE" "$IRQ_SNAP_AFTER"' EXIT
    snap > "$IRQ_SNAP_BEFORE"
    sleep "$DURATION"
    snap > "$IRQ_SNAP_AFTER"
    echo "# per-IRQ delta over ${DURATION}s (pattern: $pattern)"
    python3 - "$IRQ_SNAP_BEFORE" "$IRQ_SNAP_AFTER" <<'PYEOF'
import sys
def load(p):
    d = {}
    for line in open(p):
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        irq, counts = parts[0].strip(), parts[1].split()
        vals = []
        for tok in counts:
            try:
                vals.append(int(tok))
            except ValueError:
                break
        d[irq] = sum(vals)
    return d
before, after = load(sys.argv[1]), load(sys.argv[2])
deltas = {k: after.get(k, 0) - v for k, v in before.items()}
active = {k: v for k, v in deltas.items() if v > 0}
for irq, dv in sorted(deltas.items(), key=lambda x: -x[1]):
    mark = " (active)" if dv > 1000 else ""
    print(f"irq {irq}: delta={dv}{mark}")
if not before:
    print("matched_irqs=0 (pattern matched nothing in /proc/interrupts)")
elif active:
    mx, mn = max(active.values()), min(active.values())
    ratio = (mx / mn) if mn > 0 else float("inf")
    print(f"active_irqs={len(active)} max_delta={mx} min_delta={mn} max_min_ratio={ratio:.1f}")
else:
    print("active_irqs=0 (no IRQ activity in window)")
PYEOF
}

mode_numa() {
    local targets
    targets=$(resolve_ifaces) || return 1
    local ifc
    for ifc in $targets; do
        local base="/sys/class/net/$ifc/device"
        echo "$ifc: numa_node=$(cat "$base/numa_node" 2>/dev/null || echo unknown) local_cpulist=$(cat "$base/local_cpulist" 2>/dev/null || echo unknown) msi_irqs=$(ls "$base/msi_irqs" 2>/dev/null | wc -l)"
    done
}

mode_softirq() {
    echo "# /proc/softirqs"
    cat /proc/softirqs
}

run_mode() {
    case "$1" in
        --iface)     mode_iface ;;
        --bandwidth) mode_bandwidth ;;
        --drop)      mode_drop ;;
        --retrans)   mode_retrans ;;
        --sock)      mode_sock ;;
        --nic)       mode_nic ;;
        --rss)       mode_rss ;;
        --irq)       mode_irq ;;
        --irqdelta)  mode_irqdelta ;;
        --numa)      mode_numa ;;
        --softirq)   mode_softirq ;;
    esac
}

if [ "$MODE" = "--all" ]; then
    rc=0
    for m in --iface --bandwidth --drop --retrans --sock --nic --rss --irq --irqdelta --numa --softirq; do
        echo "========== $m ==========" >&2
        # 子 shell 隔离：模式函数内的 exit（缺工具/接口不存在等）只终止本模式，
        # 不中断整个 --all 流程；否则首个失败模式会吞掉其后所有模式
        ( run_mode "$m" ) || rc=1
    done
    exit "$rc"
fi

run_mode "$MODE"
