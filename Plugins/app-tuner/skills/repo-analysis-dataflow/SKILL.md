---
name: repo-analysis-dataflow
description: >
  
  分析代码仓库并生成数据流视图（数据管道/数据血缘）。当用户提到以下场景时触发：数据流图、数据从哪来到哪去、数据处理管道、ETL/ELT、数据血缘、数据加工流程、数据消费者、数据管道架构。产出带源码证据锚定的
  Markdown 报告：Mermaid 数据流图（阶段分带）+ 变换语义边 + 证据表。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-dataflow：数据流视图

# 什么时候用本 skill

用户想看**数据的一生**：从哪进来、经过什么变换、存到哪、谁消费。焦点是数据实体的流动，不是组件关系（那是 repo-analysis-arch）也不是调用顺序（那是 repo-analysis-sequence）。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## stages 怎么定（管道阶段是本图型的骨架）

- stage = 数据加工的**自然阶段**（采集 → 清洗 → 聚合 → 落库 → 分发），从代码流程结构读出（pipeline 函数/调度配置/主流程），不发明阶段
- 3~6 个 stage；同一 stage 内的节点是并列的同类处理

## 节点四种角色

**源**（外部输入/采集点）、**处理**（变换/清洗/计算）、**存储**（文件/DB/队列/缓存）、**消费者**（下游使用方）。

## flows 怎么认

- 一条 flow = 数据实体的一次真实搬运/变换；label 写**变换语义**（"解析 JSON"/"按日聚合"/"写入明细表"），不写"处理"
- 追踪规则：从源头函数（文件监听/API 入口/爬虫/生成器）出发，追到**持久化或离开本仓**为止；中间的纯透传折叠
- 分叉/合并如实画：一份源头数据被两条路加工 = 分叉；多源汇总 = 合并；不为简化虚构单链
- 双向往返（读写同一存储、offload/restore）合并为一条双向语义边，注记两个方向

## 存储与敏感边界

- 存储节点写真实载体（"SQLite:events 表"/"Kafka:topic-X"/"~/data/*.parquet"），引用 schema/迁移/路径常量所在行
- 敏感数据（个人信息/密钥/凭证）：边加注敏感标记，敏感判定来自字段名、处理函数名或声明（如 `sanitize`/`encrypt`/`PII`），引用判定行
- 跨进程/跨信任域的搬运：显式标注传输机制（"经消息队列"/"写共享盘"）

## 批处理 vs 流处理

- 定时批处理：注记触发方式（cron 表达式/调度配置，引用配置行）
- 持续流：节点注明"常驻"；不把批画出流的错觉

# 领域诚实约束

- 每个存储节点必须能引用到**写入代码与 schema 定义**两处证据
- 数据保留期/清理逻辑：代码里有就注明并引用；没有就声明"清理策略未在代码中发现"
- 消费者是外部系统时注明"去向不在本仓，从 <调用方式/接口> 推断"并引用依据

# IR 骨架

```json
{
  "diagram": "dataflow",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "stages": ["接入", "编排", "执行", "KV 存储", "输出"],
  "elements": [
    { "id": "client_req", "label": "HTTP 请求", "sublabel": "CompletionRequest (JSON)", "role": "source|process|store|consumer", "stage": 0,
      "sources": [{ "path": "src/server.py", "line": 53, "end_line": 64, "label": "pydantic 定义" }] },
    { "id": "f3", "role": "flow", "from": "sched_node", "to": "exec_np", "label": "PrefillBatch / DecodeBatch 张量", "variant": "emphasis|dashed" }
  ],
  "notes": ["…"]
}
```

# Mermaid 映射

```
flowchart LR
  subgraph S0["接入"]
    REQ["HTTP 请求<br/>CompletionRequest"]
  end
  subgraph S1["编排"]
    TOK["Tokenizer<br/>prompt → token ids"]
    SCH["Scheduler<br/>组批 · 分配块"]
  end
  subgraph S3["KV 存储"]
    KV[("KV Cache 块<br/>HBM 分页")]
    DDR[("DDR 分层缓存<br/>（可选）")]
  end
  REQ -->|"解析请求体"| TOK
  TOK -->|"token ids"| SCH
  SCH ==>|"批张量"| EXEC
  KV -.->|"offload / restore"| DDR
```

- stages → `subgraph` 从左到右分带；存储节点用圆柱 `[()]`
- 主数据链 `==>`；可选/旁路 `-.->`
- 双向往返（offload/restore）合并一条双向语义边

# 输出

按执行链交付：IR 落盘 → repo-view-verify 自验 → `<repo>-dataflow-view.md`（报告契约见 repo-view-render）。
