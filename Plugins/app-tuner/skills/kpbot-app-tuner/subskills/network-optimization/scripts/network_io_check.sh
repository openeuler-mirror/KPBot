#!/usr/bin/env bash
set -euo pipefail

# network_io_check.sh — 网络IO性能综合检测
# 功能：接口发现 + 中断收集 + 中断负载分析 + 丢包检测 + 报告生成
# 输出：报告保存到 /tmp/network_io_performance_report.md

echo "=== 网络IO性能检测开始 ==="
echo "检测时间: $(date '+%Y-%m-%d %H:%M:%S')"

# 步骤1: 网络接口发现
echo ""
echo "=== 步骤1: 网络接口发现 ==="

echo "=== 网络接口（Link Up）==="
ip -a link show | grep -E "^[0-9]+:.*state UP" | while read -r line; do
    iface=$(echo "$line" | grep -oP '^[0-9]+: \K\w+')
    echo "接口: $iface (UP)"
done

echo ""
echo "=== 活动网络接口（有流量）==="

mapfile -t active_iface_list < <(
    sar -n DEV 1 5 2>/dev/null | awk '
        NF >= 11 && $2 != "IFACE" && $2 != "Average:" {
            iface = $2
            ifutil = $11 + 0
            if (ifutil > 0) {
                print iface "|" ifutil
            }
        }
    ' | awk -F'|' '!seen[$1]++ { print $0 }'
)

active_ifaces=""
declare -A iface_ifutil
for entry in "${active_iface_list[@]}"; do
    iface="${entry%%|*}"
    ifutil="${entry##*|}"
    echo "接口: $iface, IFUTIL: ${ifutil}%"
    iface_ifutil["$iface"]="$ifutil"
    if [[ -z "${active_ifaces}" ]]; then
        active_ifaces="${iface}"
    else
        active_ifaces="${active_ifaces} ${iface}"
    fi
done

if [[ -z "$active_ifaces" ]]; then
    echo "错误: 未找到有流量的网络接口"
    exit 1
fi

echo ""
echo "$active_ifaces" > /tmp/active_interfaces.txt
echo "活跃接口已保存到: /tmp/active_interfaces.txt"

# 步骤2: 中断信息收集
echo ""
echo "=== 步骤2: 中断信息收集 ==="

{
    for iface in $active_ifaces; do
        echo ""
        echo "接口: $iface"

        irqs=$(grep "$iface" /proc/interrupts | awk '{print $1}' | tr -d ':')

        if [[ -n "$irqs" ]]; then
            echo "中断号: $irqs"
            numa_node=$(cat "/sys/class/net/$iface/device/numa_node" 2>/dev/null || echo "unknown")
            echo "NUMA节点: $numa_node"
            for irq in $irqs; do
                affinity=$(cat "/proc/irq/$irq/smp_affinity_list" 2>/dev/null || echo "unknown")
                echo "  中断 $irq -> 核心: $affinity"
            done
        else
            echo "未找到 $iface 的中断号"
        fi
    done
    echo ""
    echo "=== 中断信息收集完成 ==="
} | tee /tmp/irq_info.txt

# 步骤3: 中断负载分析
echo ""
echo "=== 步骤3: 中断负载分析 ==="

timeout 3 irqtop -b 2>/dev/null > /tmp/irqtop_output.txt || true

{
    if [[ -f /tmp/irqtop_output.txt && -s /tmp/irqtop_output.txt ]]; then
        echo "负载 > 10% 的中断："
        grep -E "Total|irq" /tmp/irqtop_output.txt | grep -A 1 "Total" | \
        awk '{
            if (NF >= 3) {
                irq = $1
                load = $2
                gsub(/%/, "", load)
                if (load > 10) {
                    print irq, load"%"
                }
            }
        }' | while read -r irq load; do
            echo "  $irq: $load (高负载)"
        done

        echo ""
        echo "中断负载分布："
        grep -E "Total" /tmp/irqtop_output.txt | tail -n +2 | \
        awk '{
            if (NF >= 3) {
                print $1, $2
            }
        }' | head -10
    else
        echo "irqtop 命令不可用 - 跳过中断负载分析"
        echo "替代方案：手动检查 /proc/interrupts"
    fi
    echo ""
    echo "=== 中断负载分析完成 ==="
} | tee /tmp/irq_analysis.txt

# 步骤4: 丢包检测
echo ""
echo "=== 步骤4: 丢包检测 ==="

{
    echo "=== 丢包分析 ==="
    netstat -i 2>/dev/null | grep -v "kernel" | grep -v "Iface" | while read -r line; do
        iface=$(echo "$line" | awk '{print $1}')
        rx_ierr=$(echo "$line" | awk '{print $5}')
        tx_ierr=$(echo "$line" | awk '{print $7}')
        rx_drop=$(echo "$line" | awk '{print $6}')
        tx_drop=$(echo "$line" | awk '{print $8}')
        rx_coll=$(echo "$line" | awk '{print $4}')
        tx_coll=$(echo "$line" | awk '{print $9}')

        total_errors=$((rx_ierr + tx_ierr))
        total_drops=$((rx_drop + tx_drop))
        total_collisions=$((rx_coll + tx_coll))

        echo "接口: $iface"
        echo "  RX错误: $rx_ierr, 丢包: $rx_drop, 冲突: $rx_coll"
        echo "  TX错误: $tx_ierr, 丢包: $tx_drop, 冲突: $tx_coll"

        if [[ $total_errors -gt 0 ]] || [[ $total_drops -gt 0 ]] || [[ $total_collisions -gt 0 ]]; then
            echo "  [WARNING] 检测到问题：存在错误/丢包/冲突"
        else
            echo "  [OK] 未检测到丢包"
        fi
    done
    echo ""
    echo "=== 丢包分析完成 ==="
} | tee /tmp/packet_loss.txt

# 步骤5: 生成综合报告
echo ""
echo "=== 步骤5: 生成综合报告 ==="

report_file="/tmp/network_io_performance_report.md"
report_time=$(date '+%Y-%m-%d %H:%M:%S')
report_host=$(hostname)

# 检查丢包
has_packet_loss=0
problem_ifaces=""
if [[ -f /tmp/packet_loss.txt ]]; then
    problem_count=$(grep -c "检测到问题" /tmp/packet_loss.txt 2>/dev/null || echo "0")
    if [[ "$problem_count" -gt 0 ]]; then
        has_packet_loss=1
        problem_ifaces=$(grep "检测到问题" /tmp/packet_loss.txt | awk '{print $3}' | sort -u | tr '\n' ' ')
    fi
fi

# 生成报告
{
    echo "# 网络IO性能分析报告"
    echo ""
    echo "**生成时间**: ${report_time}"
    echo "**主机名**: ${report_host}"
    echo ""
    echo "## 执行摘要"
    echo ""
    echo "系统共有 $(echo "$active_ifaces" | wc -w) 个处于 link up 状态的网络接口。"
    echo ""
    echo "## 活动网络接口"
    echo ""
    echo "| 接口 | 状态 | 流量（IFUTIL） | NUMA节点 |"
    echo "|------|------|------------------|----------|"
    for iface in $active_ifaces; do
        ifutil="${iface_ifutil[$iface]:-0.00}"
        numa_node=$(cat "/sys/class/net/$iface/device/numa_node" 2>/dev/null || echo "unknown")
        echo "| $iface | UP | ${ifutil}% | $numa_node |"
    done
    echo ""
    echo "## 中断分析"
    echo ""
    if [[ -f /tmp/irq_info.txt ]]; then
        echo "已收集中断信息，详见 /tmp/irq_info.txt"
    else
        echo "未收集到中断信息"
    fi
    echo ""
    echo "## 丢包分析"
    echo ""
    if [[ $has_packet_loss -eq 1 ]]; then
        echo "检测到问题的接口: $problem_ifaces"
        echo "- 状态: [WARNING] 存在丢包或错误"
    else
        echo "未检测到丢包"
    fi
    echo ""
    echo "## 流量速率分析"
    echo ""
    for iface in $active_ifaces; do
        echo "### $iface"
        rx_pkts_1=$(cat "/sys/class/net/$iface/statistics/rx_packets" 2>/dev/null || echo "0")
        tx_pkts_1=$(cat "/sys/class/net/$iface/statistics/tx_packets" 2>/dev/null || echo "0")
        sleep 1
        rx_pkts_2=$(cat "/sys/class/net/$iface/statistics/rx_packets" 2>/dev/null || echo "0")
        tx_pkts_2=$(cat "/sys/class/net/$iface/statistics/tx_packets" 2>/dev/null || echo "0")
        rx_rate=$((rx_pkts_2 - rx_pkts_1))
        tx_rate=$((tx_pkts_2 - tx_pkts_1))
        echo "- RX速率: $rx_rate 报文/秒"
        echo "- TX速率: $tx_rate 报文/秒"
        echo "- 总速率: $((rx_rate + tx_rate)) 报文/秒"
        rx_mbps=$((rx_rate * 1500 * 8 / 1000000))
        tx_mbps=$((tx_rate * 1500 * 8 / 1000000))
        echo "- RX: ~${rx_mbps} Mbps"
        echo "- TX: ~${tx_mbps} Mbps"
        echo ""
    done
    echo "## 结论"
    echo ""
    if [[ $has_packet_loss -eq 1 ]]; then
        echo "**系统状态**: [WARNING] 检测到丢包问题"
        echo "**影响接口**: $problem_ifaces"
        echo "**建议**: 检查驱动、硬件或网络风暴"
    else
        echo "**系统状态**: [OK] 网络运行正常"
    fi
    echo ""
    echo "## 持续监控命令"
    echo ""
    echo '```bash'
    echo "# 实时监控中断负载"
    echo "watch -n 1 'cat /proc/interrupts | grep -E \"eth|ens|eno|enp\"'"
    echo ""
    echo "# 监控网络接口"
    echo "watch -n 1 'netstat -i'"
    echo ""
    echo "# 监控流量速率"
    echo "sar -n DEV 1 5"
    echo '```'
} > "$report_file"

echo "报告文件: $report_file"
echo ""
echo "=== 网络IO性能检测完成 ==="
