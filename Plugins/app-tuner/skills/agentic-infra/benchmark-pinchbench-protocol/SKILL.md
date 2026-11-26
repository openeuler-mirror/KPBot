---
name: benchmark-pinchbench-protocol
description: >
  PinchBench benchmark（agbench 3 case 会话轨迹）的 Agent 负载回放压测协议：准备 pinchbench 轨迹
  子集、构建逐 case 镜像、在 AgentENV/E2B 自托管栈/docker 三后端上做 continuous（全后端）
  与 pause-resume（仅 aenv）回放压测与超分，采集沙箱耗时与宿主观测并产出报告。当用户要
  测/压测/调优 agbench 中 pinchbench（PinchBench）负载在宿主与沙箱上的运行性能时触发。不适用：
  agbench 其他 benchmark（走同域兄弟 skill）、官方 harness 重跑（本协议只回放录制轨迹，
  不执行 harness 与模型本身）。
---

# PinchBench Agent 负载回放压测协议

把「pinchbench 负载在这台宿主/沙箱上跑得怎么样」变成可复现、可对照的压测实验。本 skill
只负责**测量**：复用 agent-workload-benchmark-suite 的导入器、镜像构建与 runner，聚焦
pinchbench 的子集准备与差异画像；调优动作交给 kpbot-app-tuner 编排或 code-optimizer 侧
agentic-infra skill，用本协议的 run 产物做 A/B 证据。

## PinchBench 是什么、本协议测什么

3 条 case 分别对应分析/编码/研究三类能力（access log 异常分析、浏览器自动化、BYOK 最佳实践调研），是 agbench 中最异构的小池。
**优化面边界**：不覆盖推理服务吞吐（模型侧）与官方基准的任务完成/产出质量口径，只测 S1 宿主+沙箱面。

## 输入与路由

| 输入 | 必需性 | 来源与默认 |
|---|---|---|
| suite 仓库 | 必需 | clone agent-workload-benchmark-suite（个人仓，需访问授权），基准 commit `673be52`——真源：链路命令契约与口径以该仓及其 ADR-0001/0002/0003 为唯一上游，本 skill 只维护 benchmark 差异画像，不复制链路文档 |
| Python 环境 | 必需 | Python 3.13 + `uv sync --locked`（pip/venv 方式见 suite README） |
| 后端 | 三选一 | docker（本机 + `pip install docker`）/ aenv / e2b（各后端命令与凭证见 runbook §5） |
| 观测环境 | 可选 | AgentENV 所在 Linux 主机，可读 `/proc` `/sys` 与快照目录（`SNAP_BASE`/`DISKS`） |
| pinchbench 轨迹 | 默认自带 | `artifacts/traces/agbench/pinchbench_*.replay.json`；重导入（recorded/zero）见 runbook §2 |

路由与兄弟 skill 边界：agbench 其他 benchmark → 同域 `benchmark-<bench>-protocol`
兄弟 skill（按需选用）。

## 执行步骤

1. **环境准备**：runbook §1 装依赖并预检后端；凭证（`E2B_API_KEY` 等）只从环境变量或
   `--api-key-file` 注入，不写入任何文件或日志。
2. **子集准备**：runbook §3——runner 无 benchmark 过滤，须先为本轮创建**独立空子集目录**
   （mktemp，不动旧目录），只含 3 条 pinchbench 轨迹，并与 image-map 键、[画像](references/01-benchmark-profile.md)
   清单三方一致。
3. **镜像构建**：runbook §4 构建 3 个逐 case 镜像；aenv/e2b 后端先导入模板并核对就绪。
4. **回放压测**：runbook §5 三后端命令矩阵（docker / aenv / e2b，`--backend` 各自显式指定；
   continuous 全后端，pause-resume 仅 aenv），超分 `--ratios 1,2,4`。
5. **观测与报告**：压测轮自动继承 `SNAP_BASE`/`DISKS` 观测；按 runbook §6 出报告并核对
   verify 断言（末步 `python3 bench.py verify`，判定读该步 stdout）。
6. **可选 A/B**：调优前后各跑一轮同口径压测（同子集/ratios/后端/base-count），对照与
   补丁规则见 runbook §7（ADR-0003）。

## 结果分析

- 报告命令的用法、字段与口径以 [suite agent_replay README](https://gitcode.com/linhandy/agent-workload-benchmark-suite/blob/673be52/benchmark/agent_replay/README.md) 为准（真源，与基准 commit 同版），本 skill 不复制口径定义。
- 对照结论必须附轨迹子集清单、后端、ratios、repeats 与各侧轮间散布；散布超过 suite 噪声口径时降级 inconclusive。

## 验证范围

- tier-A 用例 `cases/skill-integrity`（21 项）：frontmatter 与命名、引用资源、画像与 manifest
  **按 case_id 行级逐字段对账**（fixture 模式；设 `SUITE_MANIFEST` 指向 suite 仓
  import-manifest.json 时对上游核验）、命令纯度（轨迹导入命令必含 `--benchmarks pinchbench`
  过滤、replay 文件只允许 pinchbench_*）、runbook flag/环境变量 ⊆ fixture 固化的 suite 契约
  锚点、真源 commit 引用字面钉扎（设 `SUITE_REPO` 时实证 commit 存在）、无凭据字面量
  （失败详情脱敏，不含被检值）。
- 真机三后端回放属 tier-C：需 docker/aenv/e2b 与对应宿主/服务，本 MR 保留「待真机验证」状态。

## 安全边界

- 凭证只走环境变量/密钥文件，禁止回显或落盘；观测采集限定目标主机、指定设备与快照目录；
  本 skill 不做破坏性系统变更。
- 性能补丁 A/B 遵循 ADR-0003（声明式 patch.json、独立环境名、与基线口径严格隔离）。
