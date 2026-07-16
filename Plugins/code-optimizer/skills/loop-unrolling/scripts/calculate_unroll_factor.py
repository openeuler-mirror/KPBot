#!/usr/bin/env python3
"""
计算最优循环展开系数
基于微架构信息计算最优的循环展开系数
"""

import json
import sys
import math

def calculate_unroll_factor(arch_info):
    """
    计算最优循环展开系数

    公式：最优展开系数 = 向量流水线数量 × 流水线发射宽度 × 缓存友好系数

    约束条件：
    - 展开系数必须是2的幂（2/4/8/16）
    - 不超过16
    - 如果计算结果不是2的幂，向下取最近的2的幂
    """

    # 获取微架构参数
    pipeline_width = arch_info.get("pipeline_width", 4)
    vector_pipelines = arch_info.get("vector_pipelines", 1)
    cache_info = arch_info.get("cache", {})
    l1_size = cache_info.get("l1_size_kb", 32)

    # 计算缓存友好系数
    # L1缓存较小：0.5，中等：1.0，较大：1.5
    if l1_size < 32:
        cache_coefficient = 0.5
    elif l1_size < 64:
        cache_coefficient = 1.0
    else:
        cache_coefficient = 1.5

    # 计算原始展开系数
    raw_unroll_factor = vector_pipelines * pipeline_width * cache_coefficient

    # 向下取最近的2的幂
    unroll_factor = power_of_two_floor(raw_unroll_factor)

    # 约束：不超过16
    if unroll_factor > 16:
        unroll_factor = 16

    # 生成计算逻辑说明
    calculation_logic = {
        "pipeline_width": pipeline_width,
        "vector_pipelines": vector_pipelines,
        "l1_cache_size_kb": l1_size,
        "cache_coefficient": cache_coefficient,
        "raw_unroll_factor": raw_unroll_factor,
        "nearest_power_of_two": unroll_factor,
        "final_unroll_factor": unroll_factor,
        "formula": f"{vector_pipelines} × {pipeline_width} × {cache_coefficient} = {raw_unroll_factor:.1f}",
        "explanation": [
            f"流水线发射宽度: {pipeline_width}",
            f"向量流水线数量: {vector_pipelines}",
            f"L1缓存大小: {l1_size}KB",
            f"缓存友好系数: {cache_coefficient} (基于L1缓存大小)",
            f"原始计算: {vector_pipelines} × {pipeline_width} × {cache_coefficient} = {raw_unroll_factor:.1f}",
            f"向下取最近的2的幂: {unroll_factor}",
            f"最终展开系数: {unroll_factor}"
        ]
    }

    return {
        "unroll_factor": unroll_factor,
        "calculation_logic": calculation_logic
    }

def power_of_two_floor(n):
    """返回不大于n的最大2的幂"""
    if n <= 1:
        return 1
    return 2 ** int(math.floor(math.log2(n)))

def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 从文件读取微架构信息
        with open(sys.argv[1], 'r') as f:
            arch_info = json.load(f)
    else:
        # 从标准输入读取
        arch_info = json.load(sys.stdin)

    result = calculate_unroll_factor(arch_info)
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
