---
name: kptools-py-spy
description: >
  采集 Python 进程级性能数据：Python 函数级火焰图、热点函数排名、GIL 竞争、
  子进程采样（torchrun 多 rank）、线程状态与 idle 分析（off-CPU 近似）、
  线程栈快照。当用户需要分析 Python 进程瓶颈、Python 火焰图、GIL 竞争、
  训练推理（PyTorch/torch_npu/vLLM）进程的函数级热点、torchrun 子进程采样、
  py-spy、Python 线程栈快照时触发。
license: MIT
compatibility: Linux x86_64/aarch64, py-spy 0.4.x，需 ptrace 权限
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: python_profiling
  version: "1.0.0"
---

# kptools-py-spy — Python 进程级性能采集

## 工作流程

1. 检查工具可用性与 ptrace 环境：`bash scripts/discover.sh`
2. 未安装时安装：`bash scripts/deploy.sh`
3. **语言路由（必做）**：目标进程需为 Python 进程——`tr '\0' ' ' < /proc/PID/cmdline | grep -qiE 'python[0-9.]*'`；Java → kptools-devkit java-perf，Node.js → kptools-node-profiler，C/C++ → kptools-devkit tuner hotspot
4. 执行采集：`bash scripts/collect.sh -p <PID> --<mode>`
5. 解读输出（火焰图/栈文本），配合 kptools-ascend-profiling（NPU 侧）做异构分析

## 采集命令

```bash
# Python 函数级火焰图（默认 30s，输出 SVG 路径打印到 stdout）
bash scripts/collect.sh -p <PID> -d 30
bash scripts/collect.sh -p <PID> -d 30 -o /tmp/out.svg

# raw 统计格式（文本，可 grep）
bash scripts/collect.sh -p <PID> -d 10 --format raw -o /tmp/out.txt

# GIL 竞争：只采样持 GIL 的线程
bash scripts/collect.sh -p <PID> -d 30 -g

# off-CPU 近似：包含 idle 线程栈（线程多但 CPU 低场景）
bash scripts/collect.sh -p <PID> -d 30 -i

# torchrun 多 rank 子进程采样
bash scripts/collect.sh -p <PID> -d 30 -s

# 火焰图标注线程 ID
bash scripts/collect.sh -p <PID> -d 30 -t

# Native 扩展调用栈（实验性：需 Python debug symbols，torch_npu 下效果待实测）
bash scripts/collect.sh -p <PID> -d 30 -n

# 实时热点 top（TTY 交互视图；非 TTY 自动降级为 raw 统计排序）
bash scripts/collect.sh --top -p <PID> -d 10

# 线程栈快照（瞬时，全线程 Python 栈）
bash scripts/collect.sh --dump -p <PID>
```

## 输出

- stdout: record 模式输出火焰图文件路径；dump 模式输出线程栈文本；top 降级模式输出按采样数排序的帧统计
- stderr: py-spy 诊断（采样进度/Wrote 提示）、权限与降级提示
- 退出码: 0 成功；2 依赖缺失；3 参数错误；1 采集失败

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-p <PID>` | 目标 Python 进程 | 必填 |
| `-d <sec>` | 采样时长 | 30 |
| `-o <file>` | 输出文件路径 | `./py-spy-evidence/<时间戳>/` |
| `--format <fmt>` | flamegraph / raw / speedscope / chrometrace | flamegraph |
| `-r, --rate <Hz>` | 采样频率（↑ 更密；注意 java-perf `-i ms` 方向相反） | 100 |
| `-s, --subprocesses` | 含子进程 | 关闭 |
| `-g, --gil` | 只采持 GIL 线程 | 关闭 |
| `-i, --idle` | 含 idle 线程栈 | 关闭 |
| `-n, --native` | 含 native 扩展帧（实验性） | 关闭 |
| `-t, --threads` | 火焰图标注线程 ID | 关闭 |

## 约束

- ptrace 权限：需 root，或同 uid 且 `kernel.yama.ptrace_scope=0`；容器内需 `--cap-add=SYS_PTRACE`
- 仅支持 Python 进程；非 Python PID 报错并提示语言路由
- 默认采样会短暂暂停目标进程；生产敏感场景可加 py-spy 原生 `--nonblocking`（有丢样代价，可在 collect.sh 命令后追加）
- `-n` Native：需 Python 带 debug symbols；torch_npu C++ 扩展栈可见性待实测，实测前视为实验性
- 训练推理场景建议与 kptools-ascend-profiling 配合：py-spy 看 CPU/Python 侧，ascend-profiling 看 NPU 算子/HCCL 侧

## 错误处理

- `py-spy not found` → `pip install py-spy` 或运行 `bash scripts/deploy.sh`
- `目标进程不是 Python 进程` → 执行语言路由判定，切换对应工具
- `无 ptrace 权限` → root 或 `sysctl kernel.yama.ptrace_scope=0`，容器内 `--cap-add=SYS_PTRACE`
- `top 在非 TTY 环境` → 自动降级为 raw 统计排序（stderr 标注），无需处理

## 参考

- [py-spy 使用指南](references/py-spy-guide.md)
