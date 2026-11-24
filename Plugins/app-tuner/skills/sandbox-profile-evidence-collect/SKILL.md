---
name: sandbox-profile-evidence-collect
description: 围绕调用方给定的 CubeSandbox 外部 workload 采集一次创建性能证据；只负责日志 offset/delta、轮转 warning、Host 与进程采样、metrics 和可选 bpftrace，输出 evidence 与 evidence-manifest.json。适用于已有基准命令需要可关联证据时，不执行阶段分析或报告渲染。
---

# CubeSandbox 证据采集

## 单点职责

在 workload 前后记录七类组件日志偏移并提取本次增量，同时采集 Host、process 与 metrics。不是 cube-bench 触发器；调用方通过 `--` 传入完整命令，脚本直接执行 argv，**不使用 eval**。

## 可执行入口

```bash
bash scripts/run.sh --out-dir PROFILE_DIR \
  --log-dir /data/log [--metrics-url URL | --no-metrics] \
  [--bpftrace | --bpftrace-auto | --no-bpftrace] \
  -- COMMAND [ARG ...]
```

常用采样参数为 `--host-duration`、`--host-interval`、`--process-samples`、`--process-interval`；完整参数见 `bash scripts/run.sh --help`。外部模式默认不加载 bpftrace，只有显式 `--bpftrace` 才强制要求它，`--bpftrace-auto` 仅在本机可用时尝试。

## 输入与输出

- 输入：存在的 Cubelet/CubeShim/CubeVmm/CubeMaster/CubeAPI 日志，可执行 workload；Host sampler 与 curl（`--no-metrics` 时不需要 curl）。
- 输出：`PROFILE_DIR/evidence/` 下的 `log-offsets-{before,after}.txt`、七个 `*-delta.log`、`log-rotation-warnings.txt`、Host/process/metrics 快照和 `evidence-manifest.json`。offset 使用 TSV，日志根目录和显式 API 日志路径可包含空格。
- manifest 记录脱敏 argv、退出码、每个日志的 size/inode、轮转回退和本轮产物清单。process 快照只含 `comm`、PID/TID、CPU/内存等性能字段，**不采集主机 argv**；复用 evidence 目录时会在本轮开始移除旧 bpftrace 生成物。
- 通过一键 Workflow 做外部采集时，workload 还必须在退出前向 `PROFILE_DIR/startup-latency/` 交付同一 case 的 `<case>.summary.json` 和 `<case>.json`；每轮不得复用旧 startup-latency。单独采集证据不伪造基准 JSON。

## 成功与 blocked

- 成功：退出码为 0，manifest 为 `status: success`，所有 delta 与观测产物齐全。轮转不伪装成精确 delta：复制轮转后的完整文件并写 warning，调用方必须审阅。
- workload 失败：保留全部可完成的证据和 `status: failed` manifest，并原样传播非零退出码；不得继续阶段分析。
- metrics-before 失败：写 `metrics-before-error.txt` 和 `metrics_before_exit_code`，仍执行 workload 并完成后续证据；manifest 标为 failed。workload 非零码优先原样传播，否则证据采集失败返回非零。metrics-after 同理记录独立退出码。
- blocked：缺共享 `sandbox-timeline-profile`、必需日志/采样器或显式请求的 bpftrace 时退出 2；缺 metrics 前置时只能显式 `--no-metrics` 跳过，不能静默缺失。

## 边界

本 Skill 不创建第六个基准入口，也不依赖 lifecycle 模板 Skill。原有一键 cube-bench 兼容流仍由 `sandbox-timeline-profile` 提供。
