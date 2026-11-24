---
name: kptools-ray-data-benchmark
description: 运行本地、有限资源的 Ray Data range→NumPy map_batches 微基准。用户要求 Ray Data 基准、materialize 吞吐、warmup/多轮计时或精确结果校验时立即使用；不用于容量评估或加速比结论。
license: MulanPSL-2.0
metadata:
  compatibility: Linux；Python 3.11；现有 Ray 2.56.0、NumPy 2.1.3、PyArrow 23.0.1；至少 2 CPU、256 MiB 可用 /dev/shm 与内存
  category: benchmark
  side_effect_free: 'false'
---

# Ray Data 有界微基准

## 使用

```bash
python3 scripts/benchmark.py --output-dir results/run-001
```

默认 256 行、batch 32、4 blocks、1 次 warmup、3 次测量、2 个 Ray CPU、256 MiB object store、120 秒硬墙超时。可用 `--rows`、`--batch-size`、`--parallelism`、`--rounds`、`--warmup-rounds`、`--timeout` 调整有界参数。输出目录必须全新，父目录自动创建，失败也保留 JSON 和日志。

## 测量契约

技能独立复查版本、CPU、有限内存、共享内存、输出空间、原生运算和 API；随后只启动自己的本地 Ray runtime。每阶段是确定性的 `range → map_batches(id*2+1)`，计时仅覆盖一次 `materialize()`；之后精确校验 ID、值、count、sum，并用 UDF 调用计数证明读取未触发第二次执行。原始阶段墙钟和 rows/s 全量保留，summary 排除 warmup。

初始化、调度策略和校验不计入阶段计时；本地调度开销仍包含在物化墙钟内。结果只是该配置微基准观测，不作容量、生产健康或 speedup 结论。父进程为 worker 创建独立 session，正常路径在 `finally` 调用自己的 `ray.shutdown()`；取消/超时只终止该 worker 的进程组，绝不执行全局 `ray stop` 或宽泛 `pkill`。

详见 [计时与指标参考](references/metrics.md)。
