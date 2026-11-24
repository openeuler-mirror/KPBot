# Agentic Infra 域（code-optimizer 侧）

Agent 运行时基础设施的性能优化：microVM 沙箱（CubeSandbox）与 AgentENV 沙箱的创建/恢复路径。
本插件侧负责**创建路径与内核机制**；部署、基准协议、生命周期压测、时序分析属于 app-tuner 侧的同名域（另一个 PR 迁入）。

## 成员

| Skill | 职责 |
|---|---|
| [`cubesandbox-create-optimize`](cubesandbox-create-optimize/SKILL.md) | CubeSandbox 完整创建路径与多机制组合调优：核对 Guest 预连接、Master 解析缓存与异步 spec、rootfs 影子预派生池之间的交互，做组合消融与端到端归因 |
| [`cubesandbox-restore-runtime-reuse`](cubesandbox-restore-runtime-reuse/SKILL.md) | Reuse a VMM restore runtime and replace the pool when the worker count changes. Choose this implementation or the fixed-pool device manager hunk in `scheme1-rpc.patch` for a given build. |

**场景收益 / 定位**（原插件 README 技能表条目）：面向完整创建路径与多机制组合调优，自带修订参考补丁、实施手册与消融工具；单项仅作为组合实验臂，不作为单一症状的专项入口。

本域后续迁入（逐个独立 PR）：

- code-optimizer 侧：`cubesandbox-master-create-path`、`cubesandbox-rootfs-shadow-pool`、`cubesandbox-agent-preconnect`
- app-tuner 侧（`Plugins/app-tuner/skills/agentic-infra/`）：`cubesandbox-deployment`、`cubesandbox-bench-protocol`、
  `cubesandbox-lifecycle-bench`、`sandbox-timeline-profile`、`kptools-agentenv-tuning`（含 3 个专项子技能包）、`kptools-openeuler-agentenv`

路由：多阶段联调/组合消融走本 skill；单阶段（创建路径、rootfs、预连接）后续各自成 skill 后直接进对应条目。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/agentic-infra/**`
