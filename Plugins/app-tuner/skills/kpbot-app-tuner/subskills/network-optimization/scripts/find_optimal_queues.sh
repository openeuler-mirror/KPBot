#!/bin/bash
# find_optimal_queues.sh — 自动化网卡队列数寻优
#
# 用法:
#   find_optimal_queues.sh \
#     --iface <iface> \
#     --irq-cpu-base <start_cpu> \
#     --bench-cmd '<benchmark_command>' \
#     [--prewarm-cmd '<prewarm_command>'] \
#     [--min-queues 4] [--max-queues 63] \
#     [--diag-time 20] [--verify-time 120]
#
# 输出: 最优队列数、对应 QPS、收敛过程

set -euo pipefail

# === 参数解析 ===
IFACE=""
IRQ_BASE=""
BENCH_CMD=""
PREWARM_CMD=""
APP_PATTERN=""
MIN_Q=4
MAX_Q=63
DIAG_TIME=20
VERIFY_TIME=120
TOTAL_CPUS=192
OUTDIR=/tmp/queue_find_$$

while [[ $# -gt 0 ]]; do
    case "$1" in
        --iface)       IFACE="$2"; shift 2 ;;
        --irq-cpu-base) IRQ_BASE="$2"; shift 2 ;;
        --bench-cmd)   BENCH_CMD="$2"; shift 2 ;;
        --prewarm-cmd) PREWARM_CMD="$2"; shift 2 ;;
        --app-pattern) APP_PATTERN="$2"; shift 2 ;;
        --min-queues)  MIN_Q="$2"; shift 2 ;;
        --max-queues)  MAX_Q="$2"; shift 2 ;;
        --diag-time)   DIAG_TIME="$2"; shift 2 ;;
        --verify-time) VERIFY_TIME="$2"; shift 2 ;;
        --total-cpus)  TOTAL_CPUS="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [[ -z "$IFACE" || -z "$IRQ_BASE" || -z "$BENCH_CMD" ]]; then
    echo "Usage: $0 --iface <iface> --irq-cpu-base <cpu> --bench-cmd '<cmd>'"
    exit 1
fi

mkdir -p "$OUTDIR"

# === 工具函数 ===

get_max_combined() {
    ethtool -l "$IFACE" 2>/dev/null | awk '/Pre-set maximums/{found=1} found && /Combined:/{print $2; exit}'
}

get_irq_list() {
    ls /sys/class/net/"$IFACE"/device/msi_irqs/ 2>/dev/null | sort -n
}

check_irq_balance() {
    local qcount=$1
    local irqs=($(get_irq_list))
    # Skip async IRQ (first one), bind comp IRQs
    local irq_count=${#irqs[@]}
    local comp_start=$((irq_count - $MAX_Q))  # comp IRQs start after async
    [[ $comp_start -lt 1 ]] && comp_start=1
    for ((i=comp_start; i<comp_start+qcount && i<irq_count; i++)); do
        local cpu=$((IRQ_BASE + i - comp_start))
        echo "$cpu" > /proc/irq/"${irqs[$i]}"/smp_affinity_list 2>/dev/null || true
    done
}

get_app_cpu_range() {
    local qcount=$1
    local app_start=$((IRQ_BASE + qcount))
    echo "${app_start}-383"
}

# === 单次诊断测试 ===

run_diag() {
    local qcount=$1
    local diag_dir="$OUTDIR/q${qcount}"
    mkdir -p "$diag_dir"

    echo "[$(date +%T)] Testing Combined=$qcount (IRQ=${IRQ_BASE}-$((IRQ_BASE+qcount-1)), App=$((IRQ_BASE+qcount))-383) ..."

    # 1. 设置队列数
    ethtool -L "$IFACE" combined "$qcount" 2>/dev/null || { echo "  FAIL: ethtool -L"; return 1; }

    # 2. 绑定 IRQ (停止 irqbalance, 手动绑定)
    systemctl stop irqbalance 2>/dev/null || true
    check_irq_balance "$qcount"

    # 3. 调整应用 cpuset (通过 taskset 修改所有应用进程)
    local app_start=$((IRQ_BASE + qcount))
    local app_pids=($(pgrep -f "$APP_PATTERN" 2>/dev/null || pgrep -f "postgres|mysqld|redis" 2>/dev/null || true))
    for pid in "${app_pids[@]}"; do
        taskset -pc ${app_start}-383 $pid 2>/dev/null || true
    done
    echo "  App CPUs: ${app_start}-383 (${#app_pids[@]} processes)"

    # 4. 记录 IRQ before
    cat /proc/interrupts | grep "mlx5" > "$diag_dir/irqs_before.txt" 2>/dev/null || true
    [[ ! -s "$diag_dir/irqs_before.txt" ]] && cat /proc/interrupts > "$diag_dir/irqs_before.txt" 2>/dev/null || true

    # 5. Prewarm (if provided)
    if [[ -n "$PREWARM_CMD" ]]; then
        echo "  Prewarming..."
        eval "$PREWARM_CMD" > /dev/null 2>&1 || true
    fi

    # 5. 启动 mpstat
    local irq_end=$((IRQ_BASE + qcount - 1))
    local monitor_sec=$((DIAG_TIME + 5))
    mpstat -P ${IRQ_BASE}-${irq_end} 1 "$monitor_sec" > "$diag_dir/mpstat.log" 2>&1 &
    local mp_pid=$!
    sleep 1

    # 6. 运行压测
    local start_ts=$(date +%s)
    eval "$BENCH_CMD" > "$diag_dir/bench.log" 2>&1
    local bench_rc=$?
    local end_ts=$(date +%s)
    local actual_time=$((end_ts - start_ts))

    # 7. 等待 mpstat 结束
    kill $mp_pid 2>/dev/null; wait $mp_pid 2>/dev/null

    # 8. 记录 IRQ after
    cat /proc/interrupts | grep "mlx5" > "$diag_dir/irqs_after.txt" 2>/dev/null || true

    # 9. 提取 QPS
    local qps=$(grep -oP 'queries:\s+\d+\s+\(\K[0-9.]+' "$diag_dir/bench.log" 2>/dev/null | tail -1 || echo "0")

    # 10. 分析饱和度
    local sat_count=0
    local total_samples=0
    local sum_soft=0 sum_irq=0 sum_idle=0 soft_samples=0
    awk -v qcount="$qcount" -v irq_base="$IRQ_BASE" '
    !/^$|Linux|Average/ && $2 != "CPU" {
        cpu = $(NF-10)
        idle = $NF
        irq = $(NF-5)
        soft = $(NF-4)
        if (cpu >= irq_base && cpu < irq_base + qcount && idle > 0) {
            total_irq += irq; total_soft += soft; total_idle += idle
            samples++
            if (idle + 0 < 1 && soft + irq > 30) sat++
        }
    }
    END {
        if (samples > 0) {
            printf "%.1f %.1f %.1f %d %d\n", total_irq/samples, total_soft/samples, total_idle/samples, sat, samples
        }
    }' "$diag_dir/mpstat.log" > "$diag_dir/sat.txt" 2>/dev/null

    local avg_irq=0 avg_soft=0 avg_idle=0 saturated=0
    if [[ -s "$diag_dir/sat.txt" ]]; then
        read avg_irq avg_soft avg_idle saturated samples < "$diag_dir/sat.txt"
    fi

    # 11. 分析 IRQ 均衡
    local max_min_ratio=0 active_irqs=0 total_delta=0
    python3 -c "
import sys
b=open('$diag_dir/irqs_before.txt').readlines()
a=open('$diag_dir/irqs_after.txt').readlines()
if len(b) != len(a):
    print('0 0 0')
    sys.exit(0)
deltas=[]
active=0
for bl,al in zip(b,a):
    try:
        bs=sum(int(x) for x in bl.split()[1:385])
        ae=sum(int(x) for x in al.split()[1:385])
        d=ae-bs
        if d>1000:
            deltas.append(d)
            active+=1
    except: pass
if deltas:
    deltas.sort(reverse=True)
    ratio = deltas[0]/deltas[-1] if deltas[-1]>0 else 999
    total_d = sum(deltas)
    print(f'{ratio:.2f} {min(active-1,0)} {total_d}')
else:
    print('0 0 0')
" > "$diag_dir/irq_balance.txt" 2>/dev/null

    read max_min_ratio active_irqs total_delta < "$diag_dir/irq_balance.txt" 2>/dev/null || true
    max_min_ratio=${max_min_ratio:-0}
    active_irqs=${active_irqs:-0}
    total_delta=${total_delta:-0}

    # 12. 汇总输出
    echo "  QPS=$qps | IRQ: irq=${avg_irq}% soft=${avg_soft}% idle=${avg_idle}% sat=$saturated | Balance: ratio=${max_min_ratio}x active=$active_irqs"

    # 保存结构化结果
    cat > "$diag_dir/result.json" << EOF
{"qcount": $qcount, "qps": $qps, "avg_irq": $avg_irq, "avg_soft": $avg_soft,
 "avg_idle": $avg_idle, "saturated": $saturated, "max_min_ratio": $max_min_ratio,
 "active_irqs": $active_irqs, "total_delta": $total_delta}
EOF

    echo "$qps $avg_idle $saturated $max_min_ratio"
}

# === 决策函数 ===

decide_next() {
    local qcount=$1 qps=$2 idle=$3 sat=$4 ratio=$5
    local prev_q=${prev[qcount]:-0}
    local prev_qps=${prev_qps[qcount]:-0}

    # RSS 塌缩检测
    if [[ $(echo "$ratio > 50" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        echo "STOP:rss_collapse"
        return
    fi

    # 首次运行 — 无先验信息
    if [[ "$prev_q" == "0" ]]; then
        # 饱和 → 需更多队列
        if [[ "$sat" -gt 0 ]]; then
            echo "ADD"
        else
            echo "REDUCE"
        fi
        return
    fi

    # 收敛检测
    local qps_change=0
    if [[ $(echo "$prev_qps > 0" | bc -l 2>/dev/null || echo 0) == "1" ]] && [[ $(echo "$qps > 0" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        qps_change=$(echo "scale=4; ($qps - $prev_qps) / $prev_qps * 100" | bc 2>/dev/null || echo 0)
    fi

    # 曲线见顶
    local qps_change_abs=$(echo "$qps_change" | awk '{print ($1<0)?-$1:$1}')
    if [[ $(echo "$qps_change_abs < 1" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        echo "CONVERGED"
        return
    fi

    # QPS 还在上升 → 继续当前方向
    if [[ $(echo "$qps_change > 0" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        # QPS 上升 + 饱和 → 矛盾 (可能是 RSS 改善)
        if [[ "$sat" -gt 0 ]]; then
            echo "ADD"  # 继续试探
        else
            echo "REDUCE"  # 不饱和时可降
        fi
    else
        # QPS 下降 → 方向错了
        if [[ $(echo "$qcount > $prev_q" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
            echo "CONVERGED:peak_between"  # 加队列导致 QPS 降 → 峰值在中间
        else
            echo "CONVERGED:peak_between"  # 减队列导致 QPS 降 → 峰值在中间
        fi
    fi
}

# === 主流程 ===

MAX_Q_ACTUAL=$(get_max_combined)
echo "=== NIC: $IFACE, Max Combined: $MAX_Q_ACTUAL, IRQ Base: $IRQ_BASE ==="
echo ""

declare -A prev_qps
declare -A prev
history_q=()
best_q=0
best_qps=0

diag() {
    local q=$1
    history_q+=($q)
    local result=($(run_diag $q))
    local qps=${result[0]:-0}
    local idle=${result[1]:-0}
    local sat=${result[2]:-0}
    local ratio=${result[3]:-0}

    if [[ $(echo "$qps > $best_qps" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        best_q=$q
        best_qps=$qps
    fi

    local decision=$(decide_next $q $qps $idle $sat $ratio)
    echo "  → Decision: $decision"
    echo ""

    # 存储
    prev_qps[$q]=$qps
    prev[$q]=$q

    echo "$decision"
}

# === 执行 ===

echo "--- Phase 1: 快速诊断 (${DIAG_TIME}s) ---"

# 起始值：总核数 / 4 但不超过 max/2
TOTAL_CPUS=192
START_Q=$((TOTAL_CPUS / 4))
[[ $START_Q -gt $((MAX_Q_ACTUAL / 2)) ]] && START_Q=$((MAX_Q_ACTUAL / 2))
[[ $START_Q -lt $MIN_Q ]] && START_Q=$MIN_Q

CURRENT_Q=$START_Q
DIRECTION=""
ITER=0
MAX_ITER=10

while [[ $ITER -lt $MAX_ITER ]]; do
    ITER=$((ITER + 1))
    decision=$(diag $CURRENT_Q)

    case "$decision" in
        CONVERGED*)
            echo ">>> 已收敛: $decision"
            break
            ;;
        STOP:*)
            echo ">>> 异常停止: $decision"
            best_q=$CURRENT_Q
            break
            ;;
        ADD)
            DIRECTION="up"
            NEXT_Q=$((CURRENT_Q + (MAX_Q_ACTUAL - CURRENT_Q) / 2))
            [[ $NEXT_Q -le $CURRENT_Q ]] && NEXT_Q=$((CURRENT_Q + 4))
            [[ $NEXT_Q -gt $MAX_Q_ACTUAL ]] && NEXT_Q=$MAX_Q_ACTUAL
            [[ $NEXT_Q -eq $CURRENT_Q ]] && { echo ">>> 到达上限"; break; }
            ;;
        REDUCE)
            DIRECTION="down"
            NEXT_Q=$((CURRENT_Q - (CURRENT_Q - MIN_Q) / 2))
            [[ $NEXT_Q -ge $CURRENT_Q ]] && NEXT_Q=$((CURRENT_Q - 4))
            [[ $NEXT_Q -lt $MIN_Q ]] && NEXT_Q=$MIN_Q
            [[ $NEXT_Q -eq $CURRENT_Q ]] && { echo ">>> 到达下限"; break; }
            ;;
        *)
            break
            ;;
    esac

    # 防止重复测试
    for hq in "${history_q[@]}"; do
        if [[ "$hq" == "$NEXT_Q" ]]; then
            echo ">>> 已测试过 $NEXT_Q, 停止"
            decision="SKIP"
            break
        fi
    done
    [[ "$decision" == "SKIP" ]] && break

    CURRENT_Q=$NEXT_Q
done

echo ""
echo "--- Phase 2: 验证最优值 (${VERIFY_TIME}s) ---"
echo ""

# 恢复最优队列数
ethtool -L "$IFACE" combined "$best_q" 2>/dev/null
check_irq_balance "$best_q"
sleep 2

if [[ -n "$PREWARM_CMD" ]]; then
    eval "$PREWARM_CMD" > /dev/null 2>&1 || true
fi

# 完整跑一轮
eval "$BENCH_CMD" 2>&1 | tee "$OUTDIR/verify_q${best_q}.log" | tail -20
final_qps=$(grep -oP 'queries:\s+\d+\s+\(\K[0-9.]+' "$OUTDIR/verify_q${best_q}.log" 2>/dev/null | tail -1 || echo "0")

echo ""
echo "========================================="
echo "  最优队列数: Combined=$best_q"
echo "  QPS: $final_qps"
echo "  IRQ CPUs: ${IRQ_BASE}-$((IRQ_BASE + best_q - 1))"
echo "  结果目录: $OUTDIR"
echo "========================================="
