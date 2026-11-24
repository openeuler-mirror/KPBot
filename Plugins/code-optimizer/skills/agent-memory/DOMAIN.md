# Agent 记忆域

LLM Agent 记忆框架（mem0、Zep、Letta/MemGPT、MemoRAG、Cognee 及自研 memory service）在
「框架层 → 驱动/协议 → 向量/图存储后端」这条链上的性能优化。本域只对**记忆框架这一层**
负责：框架如何构造查询/过滤/索引、如何写入后端、如何跑基准；后端引擎内部的算法
（距离计算、索引构建、图遍历）归 `vectordb/` 或 `agent-memory-db-optimization` 的边界表。

本域与 `agent-memory-db-optimization`（顶层枢纽 skill）的关系：枢纽给**诊断方法**（冻结负载 →
三段归因 → 瓶颈建模 → 分层杠杆），本域给**落在特定记忆框架（标定案例：mem0）上的可复用实现**。

## 成员

> **占位 · 当前 0 成员**。本域成员随各自独立 PR 逐个迁入，迁入后再改为可点击链接并补职责说明。

| Skill | 职责 |
|---|---|
| _（待迁入）_ | — |

### 本域后续迁入（逐个独立 PR，均只动各自的 `skills/agent-memory/**`）

- `mem0-flatidx-scalar-index` —— 记忆检索的过滤/索引下推：把 metadata JSON 里的高频过滤键提为顶层标量字段 + INVERTED 索引，候选集从全库收窄到每租户分片；标定案例 mem0 + Milvus（QA 检索吞吐 +179.6%）
- `mem0-schema-aware-seed-insert` —— 数据接口（collection schema）变更后，旁路写入路径（benchmark seed 直插）的 schema 感知对齐，避免"列数不符"崩溃
- `locomo-benchmark-replay` —— 运行 LOCOMO trace-stress-replay 记忆基准并解读四相位结果：四角色容器、manifest 单一版本源、结果文件字段与坑点

## 成员登记纪律

- 新增成员只在本文件登记，不改成魔法入口：不改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表、
  不改插件 `README.md` 的总表（共享行会让并行 PR 必然冲突）。
- 目录约定：`skills/agent-memory/<skill>/SKILL.md`；本域目录恒带 `DOMAIN.md`（安装器据此识别为分组）。
- 跨域引用：本域 skill 可引用 `vectordb/`（引擎内部，基线已有）、`perf-engineering/`（测量方法论，
  同批独立 PR 落地后有效）与 `agent-memory-db-optimization`（诊断方法），安装后统一平铺，
  按 skill 名引用即可，不写路径。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/agent-memory/**`