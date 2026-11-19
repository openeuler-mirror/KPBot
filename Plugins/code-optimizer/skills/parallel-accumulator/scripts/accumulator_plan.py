"""计算归约循环的累加器数量建议，用于隐藏浮点运算延迟。

一个串行累加器（sum += a[i]*b[i]）把吞吐限制在浮点运算的*延迟*上；
多个独立累加器让乱序执行单元重叠多条依赖链，逼近*吞吐*上限。本脚本按
「累加器数 = ceil(latency / throughput)」给出建议，并折算 SIMD lane 后的
每迭代处理元素数。仅标准库，Python 3.9+。

用法：
    python3 accumulator_plan.py --latency 4 --throughput 0.5 --simd-lanes 4
"""

import argparse
import json
import math
import sys


def recommend(latency, throughput, simd_lanes=1):
    """Return accumulator guidance for a reduction loop."""
    for name, value in (("latency", latency), ("throughput", throughput)):
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be a positive number")
    if isinstance(simd_lanes, bool) or not isinstance(simd_lanes, int) or simd_lanes < 1:
        raise ValueError("simd_lanes must be a positive integer")
    if latency < throughput:
        raise ValueError("latency must be >= throughput")
    accumulators = math.ceil(latency / throughput)
    return {
        "latency": latency,
        "throughput": throughput,
        "simd_lanes": simd_lanes,
        "accumulators": accumulators,
        "unroll_factor": accumulators,
        "elements_per_iter": accumulators * simd_lanes,
        "latency_bound": latency,
        "saturated_throughput": throughput,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latency", type=float, default=4.0)
    parser.add_argument("--throughput", type=float, default=0.5)
    parser.add_argument("--simd-lanes", type=int, default=1)
    args = parser.parse_args()
    try:
        plan = recommend(args.latency, args.throughput, args.simd_lanes)
        print(json.dumps(plan, indent=2))
    except (ValueError, OverflowError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
