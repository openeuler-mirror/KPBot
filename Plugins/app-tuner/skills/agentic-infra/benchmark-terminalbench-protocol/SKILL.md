---
name: benchmark-terminalbench-protocol
description: >
  TerminalBench benchmark（agbench 4 case 会话轨迹）的 Agent 负载回放压测协议：准备
  terminalbench 轨迹子集、构建逐 case 镜像、在 AgentENV/E2B 自托管栈/docker 三后端上做
  continuous/pause-resume 回放压测与超分，采集沙箱耗时与宿主观测并产出报告。当用户要
  测/压测/调优 agbench 中 terminalbench 负载在宿主与沙箱上的运行性能时触发。
  不适用：TB2 50 条 oracle 轨迹整场景（另立 benchmark-tb2-protocol）、agbench 其他
  benchmark（走同域兄弟 skill）、官方 terminal-bench harness 重跑（本协议只回放录制
  轨迹，不执行 harness 与模型本身）。
---

# TerminalBench Agent 负载回放压测协议

把「terminalbench 负载在这台宿主/沙箱上跑得怎么样」变成可复现、可对照的压测实验。本 skill
只负责**测量**：复用 agent-workload-benchmark-suite 的导入器、镜像构建与 runner，聚焦
terminalbench 子集准备与差异画像；调优动作交给 kpbot-app-tuner 编排或 code-optimizer 侧
agentic-infra skill，用本协议的 run 产物做 A/B 证据。

## TerminalBench 是什么、本协议测什么

TerminalBench 是开源的终端任务 agent benchmark（laude-institute/terminal-bench）：任务为
真实世界的终端操作（系统管理、CLI 工具、日志与数据处理），agent 在容器里以 shell 命令
逐条完成，官方口径是解题率。本 skill 的 4 条轨迹来自 agent 解题会话的录制导入
（ADR-0002），**不重跑官方 harness、不测解题率**——测的是宿主与沙箱回放这类终端负载时
的性能（S1）：case 吞吐、exec 耗时分布、沙箱 create/exec 计时、FC 核/容器核与
busy/stall 子窗口（口径见 suite GLOSSARY）。

**身份边界**：本 skill 只管 agbench 轨迹池中的 terminalbench 4 条导入轨迹；TB2 场景
（`artifacts/traces/tb2/` 的 50 条 oracle 轨迹，nosleep）另立 `benchmark-tb2-protocol`。
**优化面边界**：不覆盖推理服务吞吐（模型侧）与 agent 解题率，只测 S1 宿主+沙箱面。

## 输入与路由

| 输入 | 必需性 | 来源与默认 |
|---|---|---|
| suite 仓库 | 必需 | clone agent-workload-benchmark-suite（个人仓，需访问授权），基准 commit `673be52`——真源：链路命令契约与口径以该仓及其 ADR-0001/0002/0003 为唯一上游，本 skill 只维护 benchmark 差异画像，不复制链路文档 |
| Python 环境 | 必需 | Python 3.13 + `uv sync --locked`（pip/venv 方式见 suite README） |
| 后端 | 三选一 | docker（本机 + `pip install docker`）/ aenv / e2b（凭证环境变量 + 已导入模板） |
| 观测环境 | 可选 | AgentENV 所在 Linux 主机，可读 `/proc` `/sys` 与快照目录（`SNAP_BASE`/`DISKS`） |
| terminalbench 轨迹 | 默认自带 | `artifacts/traces/agbench/terminalbench_*.replay.json`；重导入见 runbook §2 |

路由与兄弟 skill 边界：TB2 整场景 → `benchmark-tb2-protocol`；agbench 其他 benchmark → 同域
`benchmark-<bench>-protocol` 兄弟 skill（按需选用）。

## 执行步骤

1. **环境准备**：runbook §1 装依赖并预检后端；凭证（`E2B_API_KEY` 等）只从环境变量或
   `--api-key-file` 注入，不写入任何文件或日志。
2. **子集准备**：runbook §3——runner 无 benchmark 过滤，须先备好**只含 4 条
   terminalbench 轨迹的子集目录**与 image-map 子集（对账见[画像](references/01-benchmark-profile.md)）。
3. **镜像构建**：runbook §4 构建 4 个逐 case 镜像；aenv/e2b 后端先导入模板。
4. **回放压测**：runbook §5 三后端 bench 命令，超分 `--ratios 1,2,4`、`--selection balanced`。
5. **观测与报告**：压测轮自动继承 `SNAP_BASE`/`DISKS` 观测；按 runbook §6 出报告并核对
   verify 断言（末步 `python3 bench.py verify`，判定读该步 stdout）。
6. **可选 A/B**：调优前后各跑一轮同口径压测（同子集/ratios/后端/base-count），对照与
   补丁规则见 runbook §7（ADR-0003）。

## 结果分析

- 报告命令的用法、字段与口径以 [suite agent_replay README](https://gitcode.com/linhandy/agent-workload-benchmark-suite/blob/main/benchmark/agent_replay/README.md) 为准（真源），本 skill 不复制口径定义。
- 对照结论必须附轨迹子集清单、后端、ratios、repeats 与各侧轮间散布；散布超过 suite 噪声口径时降级 inconclusive。

## 验证范围

- tier-A 用例 `cases/skill-integrity`（21 项）：frontmatter 与命名、引用资源、画像与 suite
  `import-manifest.json`（terminalbench 段）逐 case 对账（设 `SUITE_MANIFEST` 时对上游核验）、
  命令纯度（只允许 terminalbench 轨迹）、runbook flag/环境变量 ⊆ fixture 固化的 suite 契约锚点、
  真源 commit 存在、无凭据字面量。
- 真机三后端回放属 tier-C：需 docker/aenv/e2b 与 AgentENV 主机，本 MR 保留「待真机验证」状态。

## 安全边界

- 凭证只走环境变量/密钥文件，禁止回显或落盘；观测采集限定目标主机、指定设备与快照目录；
  本 skill 不做破坏性系统变更。
- 性能补丁 A/B 遵循 ADR-0003（声明式 patch.json、独立环境名、与基线口径严格隔离）。
