#!/usr/bin/env python3
"""A+K PGO 标准采集负载模板（CPU 路径）。

为什么存在：profile 代表性是 PGO 收益的一阶变量（384 核鲲鹏 950 实测：
matmul-only profile 使 torch.sort 比 gcc 基线慢 66%，混合形态 profile 后
反超 11%）。本脚本提供可参数化的多形态负载，避免"随手跑个窄负载"导致
PGO2 把未覆盖路径判冷去优化。

用法（PGO1 插桩版安装后）:
  export OMP_PROC_BIND=false
  export LLVM_PROFILE_FILE=/tmp/profile/default_%m_%p.profraw
  python3 ak_profile_workload.py                       # 默认 120s 均衡混合
  python3 ak_profile_workload.py --seconds 180 \
      --weights matmul:40,mlp:30,churn:15,sort:15       # 按目标负载形态加权

注意：
- 形态权重应模仿真实生产负载的算子构成（可用 perf 基线的热点分布校准）
- NPU 推理场景应改用真实模型+请求负载（见 SKILL.md「NPU 推理 PGO」节），
  本模板只覆盖 CPU 算子路径
"""
import argparse
import time

import torch

SHAPES = ("matmul", "churn", "mlp", "sort")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120, help="采集时长（默认 120）")
    ap.add_argument("--threads", type=int, default=min(64, torch.get_num_interop_threads() if hasattr(torch, "get_num_interop_threads") else 64), help="线程数（默认 min(64, 机器可用)）")
    ap.add_argument("--weights", default="matmul:25,churn:25,mlp:25,sort:25",
                    help="形态权重，逗号分隔 shape:percent，总和需为 100")
    args = ap.parse_args()

    weights = {}
    for item in args.weights.split(","):
        shape, _, pct = item.partition(":")
        shape = shape.strip()
        if shape not in SHAPES:
            ap.error(f"未知形态 {shape}（可选: {','.join(SHAPES)}）")
        weights[shape] = weights.get(shape, 0) + int(pct)
    total = sum(weights.values())
    if total != 100:
        ap.error(f"权重总和 {total} != 100")

    torch.set_num_threads(args.threads)
    print(f"[ak-profile-workload] threads={torch.get_num_threads()} weights={weights} seconds={args.seconds}")

    # 按权重生成执行序列（每轮 100 份，按百分比分配各形态调用次数）
    seq = []
    for shape, pct in weights.items():
        seq.extend([shape] * pct)

    a = torch.randn(2048, 2048)
    b = torch.randn(2048, 2048)
    x = torch.randn(256, 256)
    w1 = torch.randn(512, 784)
    w2 = torch.randn(256, 512)
    w3 = torch.randn(10, 256)
    inp = torch.randn(128, 784)
    r = torch.randn(1024, 1024)

    def run_shape(shape):
        if shape == "matmul":
            c = a @ b
            c.add_(1.0)
        elif shape == "churn":
            y = x
            for _ in range(30):
                y = y.add(1.0).relu()
            y.sum()
        elif shape == "mlp":
            h = inp.clone().requires_grad_(True)
            h1 = torch.relu(h @ w1.T)
            h2 = torch.relu(h1 @ w2.T)
            (h2 @ w3.T).sum().backward()
        elif shape == "sort":
            torch.sort(r, dim=1)

    t0 = time.time()
    rounds = 0
    counts = {s: 0 for s in SHAPES}
    while time.time() - t0 < args.seconds:
        for shape in seq:
            run_shape(shape)
            counts[shape] += 1
        rounds += 1
    elapsed = time.time() - t0
    print(f"[ak-profile-workload] done: {rounds} rounds in {elapsed:.1f}s, calls={counts}")

    # 写入 workload_manifest.json：check_profile 的形态执行门禁据此校验
    # 各形态核心函数的真实执行计数（防"采集早退/形态未跑"静默产出坏 profile）
    import json
    import os
    markers = {
        "matmul": r"gemm|addmm|mm_kernel|matmul",
        "churn": r"relu|elementwise",
        "mlp": r"backward|autograd",
        "sort": r"sort",
    }
    manifest = {
        "tool": "ak_profile_workload.py",
        "seconds": args.seconds,
        "shapes": {},
    }
    for shape, n in counts.items():
        if n > 0:
            manifest["shapes"][shape] = {
                "calls": n,
                "marker": markers[shape],
                "min_entry": max(100, n // 2),
            }
    out_dir = os.environ.get("LLVM_PROFILE_FILE", "")
    out_dir = os.path.dirname(out_dir) if out_dir else "."
    mpath = os.path.join(out_dir, "workload_manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"[ak-profile-workload] manifest 写入: {mpath} shapes={list(manifest['shapes'])}")


if __name__ == "__main__":
    main()
