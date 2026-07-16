#!/usr/bin/env python3
"""
预取优化 - perf 缓存分析脚本
解析 perf stat 输出，分析缓存效率并生成瓶颈层级判定
"""

import json
import re
import sys
import argparse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from enum import Enum

class BottleneckLevel(Enum):
    NONE = "none"
    L1_ONLY = "L1"
    LLC_ONLY = "LLC"
    L1_AND_LLC = "L1_AND_LLC"
    MEMORY_BANDWIDTH = "MEMORY_BANDWIDTH"

@dataclass
class CacheMetrics:
    cache_misses: int = 0
    cache_references: int = 0
    l1_dcache_misses: int = 0
    l1_dcache_refs: int = 0
    llc_misses: int = 0
    llc_refs: int = 0
    memory_loads: int = 0
    cpu_cycles: int = 0

@dataclass
class BottleneckAnalysis:
    level: BottleneckLevel
    priority: str  # HIGH / MEDIUM / LOW
    l1_miss_rate: float
    llc_miss_rate: float
    recommendations: List[str]

def parse_perf_stat_output(output: str) -> CacheMetrics:
    """
    解析 perf stat 输出文本
    """
    metrics = CacheMetrics()

    lines = output.split('\n')
    for line in lines:
        line = line.strip()

        # 跳过空行和表头
        if not line or '#' in line[:5] or 'Performance' in line:
            continue

        # 解析 "cache-misses" 或 "cache_references"
        if 'cache-misses' in line.lower():
            match = re.search(r'([\d,]+)\s+cache-misses', line)
            if match:
                metrics.cache_misses = int(match.group(1).replace(',', ''))

        if 'cache-references' in line.lower():
            match = re.search(r'([\d,]+)\s+cache-references', line)
            if match:
                metrics.cache_references = int(match.group(1).replace(',', ''))

        # L1 data cache
        if 'L1-dcache-load-misses' in line or 'L1-DC-load-misses' in line:
            match = re.search(r'([\d,]+)\s+L1', line)
            if match:
                metrics.l1_dcache_misses = int(match.group(1).replace(',', ''))

        if 'L1-dcache-loads' in line or 'L1-DC-loads' in line:
            match = re.search(r'([\d,]+)\s+L1', line)
            if match:
                metrics.l1_dcache_refs = int(match.group(1).replace(',', ''))

        # LLC (Last Level Cache)
        if 'LLC-load-misses' in line or 'LLC-load-misses' in line or 'LLC-load-miss' in line:
            match = re.search(r'([\d,]+)\s+LLC', line)
            if match:
                metrics.llc_misses = int(match.group(1).replace(',', ''))

        if 'LLC-loads' in line or 'LLC-load' in line:
            match = re.search(r'([\d,]+)\s+LLC', line)
            if match:
                metrics.llc_refs = int(match.group(1).replace(',', ''))

        # Memory loads
        if 'memory-loads' in line.lower():
            match = re.search(r'([\d,]+)\s+memory-loads', line)
            if match:
                metrics.memory_loads = int(match.group(1).replace(',', ''))

        # CPU cycles
        if 'cpu-cycles' in line.lower() or 'cycles' in line.lower():
            match = re.search(r'([\d,]+)\s+cycles', line)
            if match:
                metrics.cpu_cycles = int(match.group(1).replace(',', ''))

    return metrics

def parse_perf_json_output(output: str) -> Optional[Dict]:
    """
    解析 perf stat --json 输出
    """
    try:
        data = json.loads(output)
        # perf JSON 输出结构
        if 'counters' in data:
            return data['counters']
    except json.JSONDecodeError:
        pass
    return None

def calculate_miss_rates(metrics: CacheMetrics) -> Tuple[float, float]:
    """
    计算 L1 和 LLC 的 miss rate
    """
    l1_miss_rate = 0.0
    llc_miss_rate = 0.0

    if metrics.l1_dcache_refs > 0:
        l1_miss_rate = (metrics.l1_dcache_misses / metrics.l1_dcache_refs) * 100

    if metrics.llc_refs > 0:
        llc_miss_rate = (metrics.llc_misses / metrics.llc_refs) * 100
    elif metrics.cache_references > 0:
        # 如果没有 LLC refs，使用总 cache refs 估算
        llc_miss_rate = (metrics.llc_misses / metrics.cache_references) * 100

    return l1_miss_rate, llc_miss_rate

def determine_bottleneck(l1_miss_rate: float, llc_miss_rate: float) -> BottleneckAnalysis:
    """
    根据 miss rate 判断瓶颈层级

    判定规则：
    | L1 Miss Rate | LLC Miss Rate | 判定结果 | 优化优先级 |
    |-------------|---------------|---------|-----------|
    | < 5%        | < 10%         | 非缓存瓶颈 | ❌ 跳过   |
    | 5-15%       | < 10%         | L1 瓶颈  | ⭐⭐⭐     |
    | > 15%       | 10-30%        | LLC 瓶颈  | ⭐⭐⭐⭐   |
    | > 15%       | > 30%         | 内存带宽  | ⭐⭐⭐⭐⭐  |
    """
    recommendations = []

    # 计算总 miss rate
    if l1_miss_rate < 5 and llc_miss_rate < 10:
        level = BottleneckLevel.NONE
        priority = "LOW"
        recommendations.append("缓存命中率良好，无需预取优化")
    elif l1_miss_rate >= 5 and llc_miss_rate < 10:
        level = BottleneckLevel.L1_ONLY
        priority = "MEDIUM"
        recommendations.extend([
            "L1 缓存未命中率偏高",
            "建议优化数据局部性，增大单线程 working set",
            "考虑使用 software prefetch 提前加载数据"
        ])
    elif l1_miss_rate > 15 and llc_miss_rate >= 10 and llc_miss_rate <= 30:
        level = BottleneckLevel.L1_AND_LLC
        priority = "HIGH"
        recommendations.extend([
            "L1 和 LLC 缓存命中率均不理想",
            "建议：(1) 优化数据布局 (2) 增大 cache-blocking 块大小",
            "(3) 插入软件预取指令"
        ])
    elif l1_miss_rate > 15 and llc_miss_rate > 30:
        level = BottleneckLevel.MEMORY_BANDWIDTH
        priority = "CRITICAL"
        recommendations.extend([
            "内存带宽瓶颈明显",
            "建议：(1) 减少内存访问 (2) 使用更快的数据结构",
            "(3) 考虑使用 SIMD 合并内存访问",
            "(4) 软件预取可能帮助隐藏内存延迟"
        ])
    elif llc_miss_rate >= 10:
        level = BottleneckLevel.LLC_ONLY
        priority = "HIGH"
        recommendations.extend([
            "LLC 缓存未命中严重",
            "建议：(1) 优化数据布局以提高缓存利用率",
            "(2) 使用 cache-blocking 技术",
            "(3) 减少跨缓存行访问"
        ])
    else:
        level = BottleneckLevel.NONE
        priority = "LOW"
        recommendations.append("未检测到明显的缓存瓶颈")

    return BottleneckAnalysis(
        level=level,
        priority=priority,
        l1_miss_rate=l1_miss_rate,
        llc_miss_rate=llc_miss_rate,
        recommendations=recommendations
    )

def generate_analysis_report(metrics: CacheMetrics, analysis: BottleneckAnalysis) -> str:
    """
    生成人类可读的缓存分析报告
    """
    report = []
    report.append("=" * 60)
    report.append("缓存性能分析报告")
    report.append("=" * 60)
    report.append("")

    report.append("[指标统计]")
    report.append(f"  Cache Misses:        {metrics.cache_misses:,}")
    report.append(f"  Cache References:    {metrics.cache_references:,}")
    report.append(f"  L1 DCache Misses:   {metrics.l1_dcache_misses:,}")
    report.append(f"  L1 DCache Refs:     {metrics.l1_dcache_refs:,}")
    report.append(f"  LLC Misses:         {metrics.llc_misses:,}")
    report.append(f"  LLC Refs:           {metrics.llc_refs:,}")
    report.append("")

    report.append(f"[Miss Rate 计算]")
    report.append(f"  L1 Miss Rate:  {analysis.l1_miss_rate:.2f}%")
    report.append(f"  LLC Miss Rate: {analysis.llc_miss_rate:.2f}%")
    report.append("")

    report.append(f"[瓶颈判定]")
    report.append(f"  Level:    {analysis.level.value}")
    report.append(f"  Priority: {analysis.priority}")
    report.append("")

    report.append("[优化建议]")
    for i, rec in enumerate(analysis.recommendations, 1):
        report.append(f"  {i}. {rec}")

    report.append("")
    report.append("=" * 60)

    return "\n".join(report)

def main():
    parser = argparse.ArgumentParser(
        description='解析 perf stat 输出，分析缓存效率'
    )
    parser.add_argument(
        '--input', '-i',
        help='perf stat 输出文件路径（默认从 stdin 读取）'
    )
    parser.add_argument(
        '--json', '-j',
        action='store_true',
        help='输入为 JSON 格式（perf stat --json）'
    )
    parser.add_argument(
        '--report', '-r',
        help='输出报告文件路径'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='显示详细报告'
    )

    args = parser.parse_args()

    # 读取输入
    if args.input:
        with open(args.input, 'r', encoding='utf-8') as f:
            input_text = f.read()
    else:
        input_text = sys.stdin.read()

    # 解析
    if args.json:
        perf_data = parse_perf_json_output(input_text)
        if perf_data:
            # JSON 格式解析（需要转换为我们需要的结构）
            # 这里简化处理，假设 JSON 中包含相同字段
            metrics = CacheMetrics()
            for item in perf_data:
                if 'cache-misses' in item:
                    metrics.cache_misses = int(item['counter']['value'])
                # ... 更多字段解析
        else:
            print(json.dumps({"error": "JSON 解析失败"}))
            sys.exit(1)
    else:
        metrics = parse_perf_stat_output(input_text)

    # 计算 miss rate
    l1_miss_rate, llc_miss_rate = calculate_miss_rates(metrics)

    # 瓶颈判定
    analysis = determine_bottleneck(l1_miss_rate, llc_miss_rate)

    # 构建结果
    result = {
        "metrics": {
            "cache_misses": metrics.cache_misses,
            "cache_references": metrics.cache_references,
            "l1_dcache_misses": metrics.l1_dcache_misses,
            "l1_dcache_refs": metrics.l1_dcache_refs,
            "llc_misses": metrics.llc_misses,
            "llc_refs": metrics.llc_refs
        },
        "miss_rates": {
            "l1_miss_rate": round(l1_miss_rate, 2),
            "llc_miss_rate": round(llc_miss_rate, 2)
        },
        "analysis": {
            "bottleneck_level": analysis.level.value,
            "priority": analysis.priority,
            "l1_miss_rate": round(analysis.l1_miss_rate, 2),
            "llc_miss_rate": round(analysis.llc_miss_rate, 2),
            "recommendations": analysis.recommendations
        }
    }

    # 输出
    if args.verbose:
        report = generate_analysis_report(metrics, analysis)
        print(report)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 保存报告
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as f:
            if args.verbose:
                f.write(report)
            else:
                f.write(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
