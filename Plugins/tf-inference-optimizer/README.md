# tf-inference-optimizer

TensorFlow 纯 CPU 推理优化插件 — 面向鲲鹏 aarch64 的 TF 推理性能优化工作流。

仅支持 **OpenCode**（安装：`install.sh project opencode`）。

## 概述

tf-inference-optimizer 提供 7 个专业技能（skills），覆盖从环境验证到收益归因的完整优化 Pipeline：

- **环境验证**: 远程/本地环境询问与记录、binary 部署验证、进程/端口/gflags、server ready
- **多维 profiling**: saved_model.pb 静态图分析、op 级 trace、perf/topdown/objdump/内存带宽/多线程
- **优化实施**: 图优化（remapper/rewriter 算子融合）+ 自定义 fused kernel（KDNN）
- **验证归因**: 正确性用例测试（硬约束）+ op 级/端到端/容量三口径收益
- **平台支持**: 鲲鹏 aarch64（TF 2.20 自研 fork，KDNN/ANNC 加速后端）

## 技能分类

| 技能 | 阶段 | 描述 |
|------|------|------|
| `tf-inference-optimizer` | 主编排 | 4 阶段工作流编排（subagent 调度 + 状态机 + 门控） |
| `tf-env-verify` | 环境验证 | 环境询问与记录、远程链路、binary/kernel 符号、进程/端口/gflags |
| `tf-profile-collector` | 多维 profiling | 静态图 + trace + perf/topdown/objdump/内存带宽/多线程（动态 subagent 采集） |
| `tf-graph-optimize` | 图优化 | remapper/rewriter 算子融合规则 |
| `tf-kernel-optimize` | kernel | 自定义 fused op/kernel + KDNN + BUILD 链接 |
| `tf-result-verify` | 验证归因 | 正确性用例测试 + 三口径收益归因 + 负收益根因分析 |
| `tf-benchmark` | 端到端 | QPS/P99/CPU 压测 + 容量扫描 |

## 安装

### OpenCode

```bash
# 项目级安装（当前目录）
/path/to/KPBot/install.sh project opencode

# 全局安装
/path/to/KPBot/install.sh global opencode
```

验证安装：

```bash
opencode debug skill | grep tf-
```

### 本地脚本

`tf-env-verify` skill 的 `scripts/` 目录提供**脱敏脚本模板** `remote-exec.ps1` / `remote-exec.exp`（远程服务器执行入口）。模板内 IP/账号/密码均为 `<TARGET_IP>` / `<BASTION_*>` 占位符，不含真实凭据。

实际使用时，`tf-env-verify` 会在环境询问完成后自动将模板拷贝到工作目录根并填充真实信息（见 `tf-env-verify` 的 0.4 节）。填充后的副本仅存于工作目录，不写回插件。

## 使用方式

> 完整使用指南见 [`docs/usage-guide.md`](docs/usage-guide.md)。

安装后在 OpenCode 中用自然语言描述优化需求：

```
> 帮我优化 <MODEL_NAME> 模型的推理性能，目标是鲲鹏 aarch64
```

主要入口：

| 场景 | 告诉 OpenCode | 底层技能 |
|------|-------------|---------|
| 完整优化流程 | "帮我优化这个 TF 模型的推理性能" | `tf-inference-optimizer` |
| 环境验证 | "验证远程环境是否就绪" | `tf-env-verify` |
| 热点分析 | "分析这个模型的推理热点" | `tf-profile-collector` |
| 图优化 | "融合这些 MatMul 算子" | `tf-graph-optimize` |

## 优化 Pipeline 流程

完整 Pipeline 分 5 个阶段自动执行：

```
环境验证      基线建立         profiling        优化迭代(循环)       报告
┌─────────┐  ┌───────────┐  ┌──────────────┐  ┌───────────────┐  ┌────────┐
│ env-    │  │ benchmark │  │ 静态图/trace │  │ 逐优化点      │  │ 工作   │
│ verify  │→ │ 极限测试  │→ │ /perf 多维    │→ │ subagent      │→ │ 总结   │
│         │  │ 容量扫描  │  │ 采集+交叉验证 │  │ 设计→实施→验证 │  │        │
└─────────┘  └───────────┘  └──────────────┘  └───────────────┘  └────────┘
```

- **先测极限建立基线，再 profiling**：没有基线数据就优化，最后收益无法归因。
- 每个优化点基于上一优化点合入后的新 baseline 实施（滚动 baseline）。
- 正确性硬约束：正确性用例必须全部通过（口径由用户定义）。
- 收益三口径交叉印证：op 级 trace + 端到端 benchmark + QPS 容量扫描。

## 支持平台

- Kunpeng 鲲鹏 aarch64（0xd01/0xd02/0xd03/0xd06）
- TF 2.20 自研 fork（KDNN + ANNC 加速后端）

## License

[Apache-2.0](../../LICENSE)
