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

    echo "[$(date +%T)] Testing Combined=$qcount (IRQ=${IRQ_BASE}-$((IRQ_BASE+qcount-1)), App=$((IRQ_BASE+qcount))-383) ..." >&2

    # 1. 设置队列数
    ethtool -L "$IFACE" combined "$qcount" 2>/dev/null || { echo "  FAIL: ethtool -L" >&2; return 1; }

    # 2. 绑定 IRQ (停止 irqbalance, 手动绑定)
    systemctl stop irqbalance 2>/dev/null || true
    check_irq_balance "$qcount"

    # 3. 调整应用 cpuset (通过 taskset 修改所有应用进程)
    # APP_PATTERN 为空时 pgrep -f "" 会匹配所有进程（全宿主误绑核），必须给默认 pattern
    local app_start=$((IRQ_BASE + qcount))
    local app_pids=($(pgrep -f "${APP_PATTERN:-postgres|mysqld|redis}" 2>/dev/null || true))
    for pid in "${app_pids[@]}"; do
        taskset -pc ${app_start}-383 $pid >/dev/null 2>&1 || true
    done
    echo "  App CPUs: ${app_start}-383 (${#app_pids[@]} processes)" >&2

    # 4. 记录 IRQ before
    cat /proc/interrupts | grep "mlx5" > "$diag_dir/irqs_before.txt" 2>/dev/null || true
    [[ ! -s "$diag_dir/irqs_before.txt" ]] && cat /proc/interrupts > "$diag_dir/irqs_before.txt" 2>/dev/null || true

    # 5. Prewarm (if provided)
    if [[ -n "$PREWARM_CMD" ]]; then
        echo "  Prewarming..." >&2
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
    echo "  QPS=$qps | IRQ: irq=${avg_irq}% soft=${avg_soft}% idle=${avg_idle}% sat=$saturated | Balance: ratio=${max_min_ratio}x active=$active_irqs" >&2

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
    # 上一轮测试点：diag() 在命令替换子 shell 中运行，其内存状态对主 shell 不可见，
    # 故经 $OUTDIR/state_last 文件持久化（原 prev[qcount] 键取 + 子 shell 双重缺陷 → 收敛不可达）
    local prev_q=0 prev_qps=0
    [[ -s "$OUTDIR/state_last" ]] && read -r prev_q prev_qps < "$OUTDIR/state_last"

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

: > "$OUTDIR/state_best"
: > "$OUTDIR/state_last"
: > "$OUTDIR/state_history"
best_q=0
best_qps=0

diag() {
    local q=$1
    # 已测队列数落盘（huangsibeier 检视）：diag 在命令替换子 shell 运行，数组 += 对主 shell
    # 不可见（state_best/state_last 同类第三处）——防重复守卫必须经文件传递
    echo "$q" >> "$OUTDIR/state_history"
    # 先捕获完整 stdout 与退出码，校验整行后再解析（mxj1314 检视）：
    # - 不得用 local result=($(run_diag ...))：local 会掩盖返回码，失败空结果会被当四个零参与收敛
    # - 校验对象是命令的完整原始输出（恰为单行 4 个数值字段），而非重组后的变量——
    #   外部命令（taskset/systemctl 等）泄漏进 stdout 的任何一行都视为污染，拒收并停止决策
    local raw
    if ! raw=$(run_diag "$q"); then
        echo "ERROR: run_diag 执行失败 (q=$q)，本轮样本无效" >&2
        return 1
    fi
    local line_count
    line_count=$(printf '%s\n' "$raw" | wc -l)
    # 严格数值契约（mxj1314 复核）：\d+(\.\d+)? 限定位数+单小数点——"."/"1.2.3" 这类
    # 伪数值在 [\d.]+ 下会放行并作为 qps 进入计算，必须拒收
    if [[ "$line_count" -ne 1 ]] || ! printf '%s\n' "$raw" | grep -qP '^\d+(\.\d+)? \d+(\.\d+)? \d+ \d+(\.\d+)?$'; then
        echo "ERROR: run_diag stdout 样本无效（期望单行 4 个数值字段，实得 ${line_count} 行: $raw）" >&2
        return 1
    fi
    local qps idle sat ratio
    read -r qps idle sat ratio <<< "$raw"

    local prev_best_q=0 prev_best_qps=0
    [[ -s "$OUTDIR/state_best" ]] && read -r prev_best_q prev_best_qps < "$OUTDIR/state_best"
    if [[ $(echo "$qps > $prev_best_qps" | bc -l 2>/dev/null || echo 0) == "1" ]]; then
        echo "$q $qps" > "$OUTDIR/state_best"
    fi

    local decision=$(decide_next $q $qps $idle $sat $ratio)
    echo "  → Decision: $decision" >&2
    echo "" >&2

    # 上一测试点落盘（供下轮 decide_next 收敛比较；子 shell 内存状态不可达）
    echo "$q $qps" > "$OUTDIR/state_last"

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
    # 样本无效（stdout 契约失败/执行失败）→ 停止整轮寻优，不得让零值参与收敛（mxj1314 检视）
    if ! decision=$(diag $CURRENT_Q); then
        echo ">>> 诊断样本无效，停止寻优（NIC 停留在末轮测试队列数）"
        exit 1
    fi

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

    # 防止重复测试（经 $OUTDIR/state_history 文件判重——子 shell 内存的 history 数组对主 shell 不可见）
    if grep -qx "$NEXT_Q" "$OUTDIR/state_history" 2>/dev/null; then
        echo ">>> 已测试过 $NEXT_Q, 停止"
        break
    fi

    CURRENT_Q=$NEXT_Q
done

echo ""
echo "--- Phase 2: 验证最优值 (${VERIFY_TIME}s) ---"
echo ""

# 恢复最优队列数（best 状态由 diag 子 shell 经文件传递）
[[ -s "$OUTDIR/state_best" ]] && read -r best_q best_qps < "$OUTDIR/state_best"
ethtool -L "$IFACE" combined "$best_q" 2>/dev/null
check_irq_balance "$best_q"

# 恢复获胜轮的应用绑核（mxj1314 检视）：末轮 taskset 的应用 CPU 范围残留在最优队列较大时
# 会与新 IRQ 核重叠，导致最终 QPS 复验无法复现获胜配置——按 best_q 重设应用范围。
# 复核要求：恢复失败不得被 || true 静默吞掉——set 尽力执行，以读回亲和性为准做核验。
APP_RESTORE_START=$((IRQ_BASE + best_q))
APP_RESTORE_PIDS=($(pgrep -f "${APP_PATTERN:-postgres|mysqld|redis}" 2>/dev/null || true))
APP_AFFINITY_FAIL=0
for pid in "${APP_RESTORE_PIDS[@]}"; do
    taskset -pc ${APP_RESTORE_START}-383 $pid >/dev/null 2>&1 || true
    # 先判存活再读回（mxj1314 16:07 复核）：PID 已退出属压测期生命周期竞态，无害跳过；
    # 存活但读回失败或亲和性未落在获胜范围 → 计为失败，不得在亲和性未经确认时压测。
    # 存活判定用 /proc（kill -0 对他人进程返回 EPERM，会把存活误判为已退出）
    [[ -d "/proc/$pid" ]] || continue
    # 掩码取行尾字段：不依赖英文 "affinity list" 文案（非英文 locale 下 taskset 输出本地化，掩码恒在行尾）。
    # || true 必须有（huangsibeier 检视）：判活后 PID 死亡（ESRCH 竞态）时 taskset 读回 rc≠0，
    # set -euo pipefail 会直接中止脚本——WARN/ERROR 门控全部不可达；真实失败签名是"rc≠0"而非"成功但无输出"
    cur_aff=$(taskset -pc "$pid" 2>/dev/null | tail -1 || true)
    cur_aff="${cur_aff##* }"
    if [[ -z "$cur_aff" || "$cur_aff" != "${APP_RESTORE_START}-383" ]]; then
        APP_AFFINITY_FAIL=$((APP_AFFINITY_FAIL + 1))
        echo "  WARN: 存活 pid=$pid 亲和性未确认 (current: ${cur_aff:-unreadable}, 期望: ${APP_RESTORE_START}-383)" >&2
    fi
done
echo "  App CPUs: ${APP_RESTORE_START}-383 (${#APP_RESTORE_PIDS[@]} processes, fail=${APP_AFFINITY_FAIL})" >&2
if [[ "$APP_AFFINITY_FAIL" -gt 0 ]]; then
    echo "ERROR: Phase2 应用绑核核验失败（${APP_AFFINITY_FAIL} 个存活 PID 未恢复到获胜范围）——拒绝在旧 CPU 范围上做最终压测" >&2
    exit 1
fi

# 最终压测前核验：队列数必须已恢复为 best_q（未恢复的复验无意义，fail fast）
RESTORE_CHECK=$(ethtool -l "$IFACE" 2>/dev/null | awk '/Current settings/{found=1} found && /Combined:/{print $2; exit}')
if [[ -n "$best_q" && "$RESTORE_CHECK" != "$best_q" ]]; then
    echo "ERROR: Phase2 恢复核验失败 (current Combined=$RESTORE_CHECK, best_q=$best_q)——拒绝在错误配置上做最终压测" >&2
    exit 1
fi
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
