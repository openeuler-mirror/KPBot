# Agentic Infra 域（app-tuner 侧）

Agent 运行时基础设施的性能优化：AgentENV 沙箱的安装部署、内核适配与场景调优，
以及 CubeSandbox 的部署、基准协议与生命周期压测。
本插件侧负责**部署、基准协议、生命周期压测、时序分析与场景调优**；创建路径与内核
机制属于 code-optimizer 侧的同名域（`Plugins/code-optimizer/skills/agentic-infra/`，
见 !155）。

## 成员

（暂无已就位成员。本域目录下现仅有占位子目录——仅含 `.gitkeep`、尚无 `SKILL.md`，
不可作为技能加载。）

## 后续迁入（各自独立 PR）

- `kptools-agentenv-tuning`（现居 `../kptools-agentenv-tuning/`）
- `kptools-openeuler-agentenv`（现居 `../kptools-openeuler-agentenv/`）
- `cubesandbox-deployment`、`cubesandbox-bench-protocol`、`cubesandbox-lifecycle-bench`、
  `sandbox-timeline-profile`（现居本插件 `skills/` 顶层）

迁入注意：含 docker build 用例（`cases/*/case.yaml` 带 `build.context`）的技能深一层
后需同步修正 `context` 层数、`dockerfile` 前缀、Dockerfile COPY 路径与
`evals/test_case_contract.py` 的 `PLUGIN` 解析深度。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/agentic-infra/**`
