# LLM 推理性能指标体系

本文件定义 LLM 推理性能评估的核心指标体系，用于基线采集、复测对比与收益量化。

## 吞吐指标

| 指标 | 单位 | 定义 | 适用场景 |
|---|---|---|---|
| `output_throughput` | tok/s | 每秒生成的 output token 数（不含 prompt） | 离线批量推理主指标 |
| `total_throughput` | tok/s | 每秒处理的 token 数（prompt + output） | 综合吞吐评估 |
| `request_throughput` | req/s | 每秒完成请求数 | 在线服务 QPS 评估 |

## 延迟指标

| 指标 | 单位 | 定义 | 适用场景 |
|---|---|---|---|
| `TTFT` (Time To First Token) | ms | 从请求发送到收到首个 token 的时间 | 在线交互主指标；反映 prefill 延迟 |
| `TPOT` (Time Per Output Token) | ms | decode 阶段每个 output token 的平均时间 | 流式输出体验；反映 decode 效率 |
| `ITL` (Inter-Token Latency) | ms | 相邻 token 间的时间间隔（含抖动） | 流式稳定性评估 |
| `E2E latency` | ms | 单请求端到端总延迟 | 用户感知主指标 |

## 资源指标

| 指标 | 单位 | 定义 | 采集方式 |
|---|---|---|---|
| NPU utilization | % | AICore 利用率 | `npu-smi info` / `msnpureport` |
| HBM bandwidth | GB/s | HBM 内存带宽利用率 | `msnpureport` / profiler |
| HBM memory used / total | GB | HBM 显存占用 | `npu-smi info` |
| KV cache hit rate | % | prefix cache 命中率 | vLLM 日志 / metrics endpoint |
| batch utilization | % | 实际 batch / max-num-seqs | vLLM metrics endpoint |
| CPU utilization (host side) | % | Host CPU 利用率 | `top` / `mpstat` |
| Thread count | count | 进程线程数 | `ps -T` / `/proc/<pid>/status` |
| Context switch rate | cs/s | 上下文切换频率 | `pidstat -w` / `vmstat` |

## 压测工具

| 工具 | 用途 | 关键参数 | 输出指标 |
|---|---|---|---|
| `vllm.benchmark_serving.py` | 在线服务压测 | `--backend vllm-sglang`、`--dataset`、`--request-rate`、`--num-prompts` | TTFT/TPOT/ITL/E2E/throughput |
| `vllm.benchmark_throughput.py` | 离线吞吐压测 | `--model`、`--input-len`、`--output-len`、`--num-prompts` | output_throughput/total_throughput |
| `vllm.benchmark_latency.py` | 单请求延迟压测 | `--model`、`--batch-size`、`--input-len`、`--output-len` | E2E latency |

## 指标采集规范

1. **基线采集**：在未优化配置下，固定数据集 + 固定并发模式，记录上述全部指标
2. **复测对比**：每次单变量变更后，**相同数据集 + 相同并发**复测，对比关键指标
3. **交替测试**：高方差场景采用 `A B A B A B` 交替，避免时序漂移
4. **TPS 衰减警告**：连续 3 次测试结果下降超 2%，输出 `tps_decay_warning`
5. **指标记录**：吞吐用 throughput，延迟用 p50/p99 + mean，资源用 mean + peak
