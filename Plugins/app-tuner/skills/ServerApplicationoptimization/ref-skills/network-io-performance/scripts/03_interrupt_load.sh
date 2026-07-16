#!/bin/bash

# 网络IO性能检测脚本 - 中断负载分析
# 功能：检查中断负载分布并识别高负载中断

echo "=== 中断负载分析 ==="

# 使用irqtop获取中断统计（短暂运行）
timeout 3 irqtop -b 2>/dev/null > /tmp/irqtop_output.txt || echo "irqtop不可用"

# 解析irqtop输出查找高负载中断
if [ -f /tmp/irqtop_output.txt ]; then
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
    }' | while read irq load; do
        echo "  $irq: $load (高负载)"
    done
    
    # 检查负载不均衡
    echo ""
    echo "中断负载分布："
    grep -E "Total" /tmp/irqtop_output.txt | tail -n +2 | \
    awk '{
        if (NF >= 3) {
            print $1, $2
        }
    }' | head -10
else
    echo "irqtop命令不可用 - 跳过中断负载分析"
    echo "替代方案：手动检查 /proc/interrupts"
fi

echo ""
echo "=== 中断负载分析完成 ==="
echo "信息已保存到: /tmp/irq_analysis.txt"
