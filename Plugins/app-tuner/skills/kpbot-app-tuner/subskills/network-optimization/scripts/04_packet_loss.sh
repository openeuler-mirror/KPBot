#!/usr/bin/env bash
set -euo pipefail

# 04_packet_loss.sh — 丢包检测
# 功能：检查所有网络接口的丢包情况
# 依赖：01_network_interfaces.sh 生成的 /tmp/active_interfaces.txt
# 输出：丢包分析保存到 /tmp/packet_loss.txt

if [[ ! -f /tmp/active_interfaces.txt ]]; then
    echo "错误：未找到活跃接口列表"
    echo "请先运行 01_network_interfaces.sh 脚本"
    exit 1
fi

active_ifaces=$(cat /tmp/active_interfaces.txt)

{
    echo "=== 丢包分析 ==="
    echo ""

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

echo "信息已保存到: /tmp/packet_loss.txt"
