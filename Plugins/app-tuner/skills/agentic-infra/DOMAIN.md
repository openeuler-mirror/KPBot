# Agentic Infra 域（app-tuner 侧）

Agent 运行时基础设施的性能优化：AgentENV 沙箱的安装部署、内核适配与场景调优，
以及 CubeSandbox 的部署、基准协议与生命周期压测。
本插件侧负责**部署、基准协议、生命周期压测、时序分析与场景调优**；创建路径与内核
机制属于 code-optimizer 侧的同名域（`Plugins/code-optimizer/skills/agentic-infra/`，
见 !155）。

## 成员

- [`kptools-agentenv-tuning`](kptools-agentenv-tuning/SKILL.md) — AgentENV 沙箱内核适配与调优入口，含 pvspinlock / kernel-hz / cluster-tlbi / snapshot-fs 四个专项子技能包
- [`cubesandbox-deployment`](cubesandbox-deployment/SKILL.md) — CubeSandbox 单机在线部署：x86_64/ARM64 资产选择、部署预检、SHA-256 校验、官方 one-click 安装与服务健康检查；不含模板或性能测试
- `cubesandbox-lifecycle-bench`（现居 `../cubesandbox-lifecycle-bench/`）— 一键 29 档 Workflow、路由与共享执行器
- CubeSandbox 十个单项基准入口（现居本插件 `skills/` 顶层）：
  `cubesandbox-bench-preflight`、`cubesandbox-bench-prepare`、
  `cubesandbox-template-create-bench`、`cubesandbox-density-bench`、
  `cubesandbox-snapshot-bench`、`cubesandbox-dirty-page-bench`、
  `cubesandbox-snapshot-create-bench`、`cubesandbox-rollback-bench`、
  `cubesandbox-clone-bench`、`cubesandbox-pause-resume-bench`

> `cubesandbox-deployment` 的定位（原插件 README 技能表条目）：先沙箱装得上、装得对，才谈得上
> 基准与压测；装不上、装错版本、资产校验失败都从这里查。

## 域内路由

- 沙箱装不上 / 装错版本 / 资产校验失败 → `cubesandbox-deployment`
- 只要只读环境证据 → `cubesandbox-bench-preflight`；只做已授权 NUMA/TAP 准备 →
  `cubesandbox-bench-prepare`
- 只测一个生命周期场景 → 对应 `cubesandbox-*-bench` 单项入口；要完整 29 档或跨章节自定义子集 →
  `cubesandbox-lifecycle-bench`
- AgentENV 内核适配与专项调优（TLBI、kernel-hz、pvspinlock、snapshot-fs）→ `kptools-agentenv-tuning`
- 改创建路径与内核机制本身 → `code-optimizer` 侧 `agentic-infra/`

## 后续迁入（各自独立 PR）

- `kptools-openeuler-agentenv`（现居 `../kptools-openeuler-agentenv/`）
- `cubesandbox-bench-protocol`、`cubesandbox-lifecycle-bench` 及上述十个单项基准入口、
  `sandbox-timeline-profile`（现居本插件 `skills/` 顶层）

迁入注意：含 docker build 用例（`cases/*/case.yaml` 带 `build.context`）的技能深一层
后需同步修正 `context` 层数、`dockerfile` 前缀、Dockerfile COPY 路径与
`evals/test_case_contract.py` 的 `PLUGIN` 解析深度。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/agentic-infra/**`
