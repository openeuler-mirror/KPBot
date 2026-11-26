#!/usr/bin/env bash
set -euo pipefail

# 03_interrupt_load.sh — 中断负载分析
# 功能：检查中断负载分布并识别高负载中断
# 输出：中断负载分析保存到 /tmp/irq_analysis.txt

{
    echo "=== 中断负载分析 ==="
    echo ""

    timeout 3 irqtop -b 2>/dev/null > /tmp/irqtop_output.txt || true

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

echo "信息已保存到: /tmp/irq_analysis.txt"
