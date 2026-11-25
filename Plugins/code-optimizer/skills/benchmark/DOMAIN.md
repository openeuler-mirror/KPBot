# mem0 + Milvus 基准 harness 工程域

**本域限定于 mem0 + Milvus 联调项目的 LOCOMO trace-stress-replay benchmark harness**（`AgentMemoryWorkload`
fork 的 runner 演进），不承诺对任意基准通用。回答的是这个 harness 这一侧的五个工程问题：怎么把"测起来
时快时慢、结果不可比"的 mem0/milvus 负载做成"相位干净、数据池一致、拓扑可控、统计可信"的多轮可复现结果。

与 `perf-engineering/`（优化流程的测量方法论，同批独立 PR 落地后有效）互补：本域管"怎么生成并跑出一轮可信的 mem0/milvus 负载"，
后者管"拿到负载后怎么采样、归因、证伪、决策"。

标定案例：mem0（记忆层）+ Milvus（向量库）联调项目的 benchmark 子仓，其 runner 从单实例演进到
多 runner + 相位隔离 + 黄金卷 + 统计设计，沉淀为下面五个原子 skill。所有机制（barrier、黄金卷、seed 稳定、
NUMA 拓扑、统计口径）都针对 mem0/milvus 的向量检索 + 索引构建负载形态设计。

## 适用边界

- **只覆盖 mem0 + Milvus 的 LOCOMO harness**：相位命名（ingestion/qa）、索引就绪（HNSW）、黄金卷（Milvus 卷）、
  seed（trace 重放建库）都是 mem0/milvus 特有概念。
- 迁移到其它基准（非 mem0/milvus）时，相位划分、索引就绪判据、数据池固化方式需按目标负载重行定义，
  不能直接套用本域命令/开关名。

## 与相邻域的边界

| 维度 | 本域（benchmark） | perf-engineering | vectordb | agent-memory |
|---|---|---|---|---|
| 关注点 | mem0/milvus 负载生成 / 相位划分 / 数据池一致性 / 拓扑绑定 | 采样 / 归因 / 证伪 / 量化决策 | Milvus 引擎内部优化（距离计算内核、索引构建、图遍历），专项见 `vectordb/DOMAIN.md` | 记忆框架层优化（查询/过滤/索引/写入路径）+ 框架侧基准运行（locomo-benchmark-replay） |
| 典型产出 | 一轮可比的结果（phase_windows / 分相位吞吐） | 瓶颈证据（火焰图 / 每请求成本） | 引擎内核 patch + 收益（如 SVE 距离内核、Go 分配削减） | 框架侧代码 patch + 收益 |

> **与 agent-memory 的边界（同一 LOCOMO harness 的两个视角）**：本域管 harness **工程侧**——怎么把负载切成
> 相位干净/数据池一致/拓扑可控/统计可信的多轮结果（phase-isolation / golden-volume / statistical-design
> 等）；`agent-memory/locomo-benchmark-replay` 管 harness **运行侧**——怎么跑起来、怎么换镜像、怎么读结果文件
> 与坑点。二者互补、不重复：一个"改 harness 让结果可信"，一个"用 harness 验证框架改动"。

## 成员

| Skill | 职责 |
|---|---|
| [`benchmark-multirunner-topology`](benchmark-multirunner-topology/SKILL.md) | 多 runner 分片 + NUMA 拓扑：MEM0/RUNNER 倍数关系、共享/独占形态、strict/floating/none、内存节点回退、MOCK 分流 |
| [`benchmark-phase-isolation`](benchmark-phase-isolation/SKILL.md) | 相位隔离：barrier / phase 开关 / AsyncJsonlWriter，把 mixed 模式重叠的 ingestion/qa 切成干净窗口，用 phase_windows 定界 |
| [`benchmark-golden-volume-replay`](benchmark-golden-volume-replay/SKILL.md) | 黄金卷 + clone 复用轮：build 轮 finalize+seal 固化 Milvus 底库卷，复用轮 probe→clone→跳过 seed，多轮数据池逐字节一致 |
| [`benchmark-seed-stability`](benchmark-seed-stability/SKILL.md) | mem0/milvus seed 稳定性：关闭 auto compaction、seed 资源 boost、index-ready 超时与 search-recovery 等待（规避 MilvusException code=106） |
| [`benchmark-statistical-design`](benchmark-statistical-design/SKILL.md) | mem0/milvus 统计设计：组内/组间 CV、数据池一致、黄金卷前缀隔离、方案 C（存档轮 + clone 数据点） |

> 成员逐个独立 PR 迁入：本表成员链接的 skill 目录随各自 PR 落地，合入顺序靠逐一协调（本域 DOMAIN.md 只登记一次，
> 成员 PR 不复改本文件）。

推荐使用顺序：先定拓扑 → 切相位 → 固化底库 → 压住 seed 抖动 → 定统计口径（对应上表五行，自上而下）。

## 成员登记纪律

- 新增成员只在本文件登记，不改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表、不改插件 `README.md` 总表。
- 目录约定：`skills/benchmark/<skill>/SKILL.md`；本域目录恒带 `DOMAIN.md`。
- 标定案例数字是 mem0/milvus 负载下收益分布/口径形态，不是对其它基准的承诺。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/benchmark/**`