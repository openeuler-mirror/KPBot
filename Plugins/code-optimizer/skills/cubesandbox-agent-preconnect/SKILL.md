---
name: cubesandbox-agent-preconnect
description: 诊断并优化 CubeSandbox 快照恢复中的 Guest agent 预连接、监控连接复用与 restore runtime 复用。适用于 RestoreVm 后串行握手、重复建连或重复构造 Tokio runtime；独立 Guest RPC 并行化不属于本技能。
---

# CubeSandbox Guest 预连接与运行时复用

缩短恢复到 Guest 可用的关键路径，并降低重复初始化导致的长尾。先证明存在可重叠的等待，再决定是否实施；阶段改善不代表整批创建时间下降。

## 输入与准备

获取 CubeSandbox 源码路径、commit、目标机器、创建场景、现有 trace 与独立 `run_dir`。识别 `CubeShim/shim/src/sandbox/sb.rs` 和 `hypervisor/vmm/src/device_manager.rs` 的实际实现。已经存在相同优化时记录 `already_present`，不重复应用。

本目录独立包含 [原理与实施](references/implementation.md)、[逐步操作手册](references/runbook.md)、[复现协议](references/reproduction.md)、原始补丁和分析工具。先按操作手册确认源码与环境，再按机制实施；不依赖其他技能或个人目录。

## 工作流程

开始实施前按以下定位表读取源码，不能只按 patch 行号操作：

| 入口 | 当前行为 | 目标行为 |
|---|---|---|
| `create_sandbox` | restore 后才建连 | restore 期间有界等待 agent，之后汇合 |
| `connect_agent` | 建连并安装 client/conn | 分离安装步骤，允许复用预连接结果 |
| `monitor_vm` | 可能重复建连接 | 在传输语义允许时复用已安装连接 |
| `DeviceManager` restore | 每次构建 runtime | 在可复用的进程作用域内复用 |

1. **分段归因**：区分 VM restore、socket 建立、vsock CONNECT、Guest 等待、时钟/RNG RPC、monitor 连接及 runtime 构造。计算真正可重叠窗口，不能把所有 Guest RPC 时长当成预连接收益。
2. **分开实验**：预连接、monitor 连接复用、runtime 复用分别保留开关或独立补丁；不顺带提高设备恢复并发度。
3. **实现预连接**：使用有截止时间的重试；成功连接必须属于本次 VM 实例。restore 成功后汇合；失败、取消和超时都停止并回收子任务。预连接不可用时回到串行连接，遵守整个创建请求的剩余 deadline。
4. **实现复用**：确认 ttrpc Client 的 clone 允许健康监控与业务请求共享传输；确认共享 runtime 的进程作用域、线程数初始化和调用上下文允许复用，避免在其 executor 线程里嵌套 `block_on`。
5. **正确性验证**：覆盖正常创建/删除、Guest 迟就绪、连接失败、预连接成功后 VM 失败、父请求取消、重复创建 ID/新 VM 实例、monitor 重连、连续 restore。验证无遗留重试任务和连接泄漏。
6. **性能验证**：交错比较基线与候选，主指标为整批 `total_time`；同时记录连接等待和请求长尾。CI 跨零时保留 `inconclusive`，可单独报告长尾变化，不宣称创建总时间提升。

## 交付

按 [操作手册](references/runbook.md) 的“源码定位→分段计时→三步实现→构建发布→故障矩阵→消融”执行，每步保留输入、命令与观察。历史参数为 retry 1ms、预连接尝试上限 30s、restore 后 join 上限 10s；迁移时受当前请求总 deadline 约束，不能再额外串行等待满额超时。

本包自带 `scripts/plan_comparison.py`、`scripts/compare_create_time.py` 和 `cases/comparison-contract/test_compare.py`。在本技能目录运行，采集计划和结果输出到外部 run_dir；详细参数见 [本地复现协议](references/reproduction.md)。

在 `run_dir` 留存诊断证据、按机制拆分的补丁、构建和运行二进制哈希、正确性记录、原始 bench JSON 与配对结果。输出状态使用 `already_present / not_applicable / awaiting_target_evidence / rejected / inconclusive / accepted`，并给出原因。

## 历史参考

[scheme1-rpc.patch](references/scheme1-rpc.patch) 是用户提供的 CubeSandbox 0.7.1 历史实现，不是可直接部署的当前版本。它没有完整处理 `start_vm().await?` 提前失败后的预连接任务回收，迁移时必须补齐。
