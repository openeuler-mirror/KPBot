---
name: kptools-tpsplit-optimization-verify
description: >-
  TP 拆分 / TP 拓扑迁移部署完成后的交付验证（L4-L6）：L4 功能测试（正确性 / max-model-len 边界 / 解析器）、L5 性能对比（Prefill 吞吐 / TTFT / KV 传输带宽，对比部署时停服前归档的旧基线，同口径）、L6 稳定性观察（KV 报错率 / 偶发 NPU 错误 / DP 掉员 / 实例重启）。当用户提到以下场景时使用本技能：TP 拆分后验证、TP 拓扑迁移完成后的分层测试、L4/L5/L6、迁移后功能/性能/稳定性验证、新旧集群性能对比、TP8 迁移完效果实测、按部署指南做交付验证。本技能是只读验证面（零变更：不改配置、不重启、不切流、不回滚），不达标时给出回滚/调参建议并转 kptools-tpsplit-optimization（配套 PR #442 交付） 执行。集群还没拉起 / L0-L3 拉起验证归 kptools-tpsplit-optimization（配套 PR #442 交付）；TP 可行性测算归 kptools-tpsplit-check；TTFT 阶段拆解收益对比报告归 kptools-tpsplit-report。
license: Proprietary
compatibility: 远端 Linux（openEuler 等，aarch64）NPU 环境：SSH 只读访问（日志/配置读取）+ 向目标服务发测试/压测请求（发请求是验证动作，会产生真实推理负载，执行窗口与压力档位经用户确认）；压测用现场已有工具（如 manual_benchmark_existing_service.py）；L6 需要跨观察窗口的日志/监控读取权限。标准输入是 kptools-tpsplit-optimization（配套 PR #442 交付） 产出的部署指南。
metadata:
  skill_type: tool_assisted
  category: verification
  domain: tp_topology_verification
  activation_mode: active
  version: "0.1.0"
---

# kptools-tpsplit-optimization-verify：TP 拓扑迁移交付验证（L4-L6，只读）

承接 `kptools-tpsplit-optimization`（配套 PR #442 交付）（deploy）的交接：新集群已拉起、L0-L3 拉起验证已通过、网关已切流。本 skill 做**交付前验证**——L4 功能 / L5 性能对比 / L6 稳定性观察——逐层执行（不过不进下一层），产出《验证报告 `.md`》，判定「可交付 / 建议调参 / 建议回滚」。

## 零变更红线（先读）

本 skill 是**只读验证面**，全程不做任何变更：

- 不改配置、不重启实例、不切流、不回滚、不停服——这些是 `kptools-tpsplit-optimization`（配套 PR #442 交付） 的执行面
- 发现不达标：给出判定与建议（调参项、回滚步骤引用部署指南），**转 deploy 决策与执行**，本 skill 不代执行
- 测试/压测请求会对目标服务产生真实推理负载：执行窗口、压力档位、L6 观察时长经用户确认后再发

## 输入契约（与 deploy 的交接）

标准输入 = deploy 产出的《部署指南 `.md`》：

| 必需信息 | 来源 | 缺失处理 |
| --- | --- | --- |
| 集群访问方式（网关地址或直连实例地址） | 部署指南 | 必须问用户 |
| 实例布局表（主机/卡/端口） | 部署指南 | 问用户；报告标注「输入降级」 |
| L0-L3 结果 | 部署指南 | 缺失即转 deploy 补做——不在未过 L3 的集群上做 L4-L6 |
| 关键参数（max-model-len / max-num-seqs / parser 类型） | 部署指南或旧脚本 | L4 边界与解析器测试需要，缺则问用户 |
| 旧基线数据 + 口径（数据集/请求长度分布/并发数/时长/预热/统计窗口） | 部署指南归档（deploy 停服前采样） | L5 无基线时降级为「现状实测、不判定达标」，向用户说明 |

没有部署指南（他人执行 / 变更单流程）：按上表逐项向用户收集等价信息，验证报告标注「输入降级：无部署指南」；不编造 L0-L3 结论，拿不到就先转 deploy 补拉起验证。

## 快速决策

1. 有部署指南 + 部署/切流完成 → 完整 L4→L5→L6（逐层，不过不进下一层）
2. 只要做某一层（如只要性能对比）→ 单层执行，报告其余层标注「未覆盖」
3. 集群还没拉起 / L0-L3 没过 → 转 `kptools-tpsplit-optimization`（配套 PR #442 交付），本 skill 不做拉起期验证
4. L5 不达标 / L4 发现容量问题 → 判定 + 建议（容量参数复核指向 deploy 的业务参数复核环节；调参/回滚决策与执行转 deploy），与用户共同决策
5. 要正式的 TTFT 阶段拆解收益对比报告（两套数据目录出 .md/.html/.json 三件套）→ 转 `kptools-tpsplit-report`；本 skill 的 L5 只做现场达标判定
6. 迁移后出现 KV 报错 / NPU 偶发错误 / hangs（故障排查，非计划性观察）→ 转 `kptools-tpsplit-optimization`（配套 PR #442 交付）（配置错配根因排查 + 回滚执行面）

## L4-L6 总览（细则见 references/verification-details.md）

| 层 | 内容与通过判据（简） |
| --- | --- |
| L4 功能 | 正确性 / max-model-len 边界 / parser（tool-call、reasoning）各一例，行为与旧集群一致 |
| L5 性能对比 | 吞吐 / TTFT / KV 带宽**对比停服前归档的旧基线**（同负载/并发/计时口径）：达标或给出调参/回滚建议 |
| L6 稳定性 | ≥24h 或用户窗口观察 KV 报错率 / 偶发 NPU 错误 / DP 掉员 / 实例重启，通过才算交付完成 |

失败处理：L4 失败 → 容量/参数问题（max-model-len / max-num-seqs / max-num-batched-tokens）转 deploy 复核重议，议后重新 L4；L5 不达标 → 与用户共同决策调参（转 deploy）或回滚（转 deploy，步骤引用部署指南回滚章节）；L6 异常 → 按异常类型转 deploy（KV/NPU 偶发错误按其迁移后故障路径排查）或继续观察（用户拍板）。

## 交付

《验证报告 `.md`》（用户指定目录或部署目录）。**报告固定结构（6 节 + 分节判定的逐字段
定义）以 `references/verification-details.md` §报告结构为唯一权威**——本文件不再
复述（避免逐字双份维护漂移，Tier2 已判定双份 DUPLICATE-HIGH）；生成时逐节对照该参考
文档，不得增删顶层章节、不得合并或拆动章节（增补内容放对应章节内）。

应要求把 L4-L6 结果回填部署指南对应章节（只回填该文档既定章节，改前备份原文件）。

## 常见坑

- **旧基线必须同口径对比**：TP8 与 TP32 负载模型不同，历史压测数据不能沿用；只有 deploy 停服前按当时口径采的那份可用。基线的负载/并发/计时口径（数据集、请求长度分布、并发数、时长、预热、统计窗口）从部署指南归档读取，L5 按相同口径测新集群。
- **无基线 ≠ 随便比**：没有归档基线时只报新集群现状（吞吐/TTFT/带宽实测值），不做达标判定，更不要拿历史数据硬凑对比。
- **L4 边界测试避开业务高峰**：接近 max-model-len 的长请求与压满 max-num-seqs 的并发对在线服务有真实压力，窗口经用户确认。
- **L6 的观察项含已知环境偏斜**：mooncake_master 与引擎跨 python/运行时版本等环境差异列入观察项——历史能跑通不代表升级后能跑通；此类异常转 deploy 回归 KV 链路（L3 判据在 deploy 侧）。
- **观察类指标用日志/监控读取，不发压测流量**：L6 是被动观察，不要为「看稳定性」主动制造压测。
- **验证请求要经真实路由**（网关或部署指南声明的测试路由）：直连单个实例拿到的结果不代表端到端行为。

## 边界与配套拆分说明

- 本 skill 由 `kptools-tpsplit-optimization`（配套 PR #442 交付）（原部署+验证一体，v0.5.x 及之前 L0-L6 全包）拆分而来：deploy 保留 L0-L3 拉起验证与全部执行面，本 skill 承接 L4-L6 交付验证。两 PR 建议同批合入，避免过渡期 L4-L6 触发面悬空。
- 与 deploy 的交接：输入 = 部署指南（布局表/修改点清单/L0-L3 结果/备份位置）+ 停服前旧基线归档；输出 = 验证报告 + 不达标时的调参/回滚建议（执行回 deploy）。
- 相邻边界：TP 可行性测算归 `kptools-tpsplit-check`；TTFT 阶段拆解收益对比报告归 `kptools-tpsplit-report`（本 skill L5 的现场达标判定与它的正式收益报告互补）；TTFT 打点日志采集/解析归 kptools-tpsplit-log-collector / kptools-tpsplit-log-analysis。

## 文件

- `references/verification-details.md` — L4/L5/L6 执行细则与通过标准、基线口径、验证报告结构
- `evals/evals.json` — 行为评测集（完整验证 / 单层 / 输入降级 / 边界转介）
- `evals/eval_queries.json` — 触发评测集（正负例路由矩阵）
