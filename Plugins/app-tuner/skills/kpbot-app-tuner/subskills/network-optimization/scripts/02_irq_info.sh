#!/usr/bin/env bash
set -euo pipefail

# 02_irq_info.sh — 中断信息收集
# 功能：收集活跃网络接口的中断号及其 CPU 亲和性
# 依赖：01_network_interfaces.sh 生成的 /tmp/active_interfaces.txt
# 输出：中断信息保存到 /tmp/irq_info.txt

if [[ ! -f /tmp/active_interfaces.txt ]]; then
    echo "错误：未找到活跃接口列表"
    echo "请先运行 01_network_interfaces.sh 脚本"
    exit 1
fi

active_ifaces=$(cat /tmp/active_interfaces.txt)

{
    echo "=== 活动接口的中断信息 ==="
    echo ""

    for iface in $active_ifaces; do
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
        echo ""
    done

    echo "=== 中断信息收集完成 ==="
} | tee /tmp/irq_info.txt

echo "信息已保存到: /tmp/irq_info.txt"
