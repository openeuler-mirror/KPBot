---
name: agent-memory-optimize
description: >
  编排 Agent 记忆系统 / 图数据库后端的整体优化流程：冻结确定性负载 → 客户端/驱动/服务端三段归因 →
  瓶颈建模 → 分层杠杆实施 → 矩阵复测与跨架构对照。作为该技能族的统一入口，负责边界判定、
  客户端运行时适配与出界路由；单个阶段的可执行细节由各专项 skill 承担。
  场景入口类触发：Agent 记忆系统优化、知识图谱数据库优化、Graphiti/Neo4j 慢、memory backend 调优、
  ingest/search 基准。编排类触发：记忆后端优化怎么开始、优化流程、先做哪一步、该用哪个子 skill。
---

# Agent 记忆系统优化（编排）

本 skill 是入口：只做三件事——**判边界、判运行时、排阶段**。具体怎么做由专项 skill 负责，
不在这里重复。

## 输入

| 字段 | 必填 | 说明 |
|---|---|---|
| `repo` | 是 | 记忆框架源码路径；客户端与服务端分别记录 |
| `store` | 是 | 后端类型与版本（属性图库/向量库/关系库），是否可重建原生二进制 |
| `workload` | 是 | 写入集（会话/文档数）与查询集（条数、并发、检索配方） |
| `bench_cmd` | 是 | 可运行的基准命令 |
| `model_calls` | 是 | LLM/embedding/rerank 缓存；无缓存先做 `llm-call-replay-cache` |
| `goal` / `constraints` | 建议 | 延迟/吞吐/CPU/内存目标；能否改 schema、二进制、核预算 |

## 五阶段与对应 skill

| 阶段 | 目标 | 交给 |
|---|---|---|
| 0 | 冻结确定性负载 | `agent-memory-workload-freeze` → `llm-call-replay-cache` → `llm-cache-canonicalization` → `agent-memory-bench-gate` |
| 1 | 拆清框架/驱动/服务端 | `agent-memory-cost-attribution`（+ `agent-memory-read-scale-curve`） |
| 2 | 定位到指令/访存/函数 | `agent-memory-topdown-analysis` → `agent-memory-cache-miss-analysis` → `graph-server-flamegraph` → `agent-memory-bottleneck-classification` |
| 3 | 按 ROI 实施杠杆 | `agent-memory-payload-slimming` → `bolt-decode-offload` → `temporal-hydration-offload` → `agent-memory-read-scaling` → `graph-store-readpath-patch` → `agent-memory-runtime-config` → `agent-memory-retrieval-recipe` |
| 4 | 出可交付结论 | `agent-memory-bench-matrix`；被证伪方向查 `agent-memory-negative-results` |

L8 过滤/索引下推（mem0 + Milvus 形态：把 metadata 高频过滤键提为 collection 顶层标量字段 + INVERTED 索引）不在本族 L1–L7 内，归属 `agent-memory` 域的 `mem0-flatidx-scalar-index`（待迁入，见 #279）；本族只负责判定这一层是否成立，不重复它的实现。

任何阶段发现"负载不可复现"（同配置重跑 phase 波动 > 2%）→ 退回阶段 0（冻结负载）。

## 运行时适配（先判这个，再选杠杆）

| 客户端运行时 | 首选顺序 | 基本无效 |
|---|---|---|
| Python / TS / Node | 载荷 → 解码下沉 → 水合下沉 → 多进程 → 服务端 | 多线程（GIL）、纯 Python 内微优化 |
| C++ | 载荷 → 检索配方 → 服务端/存储引擎 → 线程与分片 → 分配/拷贝消除 | 换编译语言做"解码下沉" |
| Rust | 同 C++：先查 `clone`/中间结构物化 | 语言下沉 |
| Go | 载荷 → 消除 `interface{}` 装箱与反射解码 → 服务端 → 并发/GC | 语言下沉 |
| JVM | 载荷 → 装箱/临时对象/复用缓冲 → 服务端同进程直调 → GC | 换语言做解码 |
| 同进程/嵌入式 | 查询计划 → 少物化/视图替代拷贝 → 引擎内部 → 并发 | 解码下沉、载荷瘦身 |

判据：**收益 ≈ 该层耗时占比 × 该运行时可削比例**。解释型客户端解码层常占 60~90%，
编译型可能只有 5~15%，同一手法价值差一个数量级。

## 职责边界

| 情况 | 去哪里 |
|---|---|
| 向量索引库内部算法（ANN 索引、距离计算） | `vector-search-optimize` / `faiss-optimize` |
| 引擎跨版本迁移、patch 移植 | `vector-db-optimization-migration` |
| 数据库服务端配置与内部状态（pagecache、锁等待、MySQL） | app-tuner 的 `database-workload-analysis` |
| 模型推理/训练本身 | `tf-inference-optimizer`、`ort-kdnn-attention-opt`、`kunpeng-llama-cpp-tuning` |
| 纯访存布局改写（AoS→SoA、分块） | `memory-access-optimization` |
| Go profiling 工具细节 | `go-pprof-analysis` |
| 热点落在系统库且可能"优化已存在只是未加载" | `hot-library-version-gap` |

## 协作指针

- 平台层（BIOS/OS/NUMA/绑核/性能库）：`kpbot-app-tuner`
- ARM 专项采集（SPE、微架构参数）：`arm-spe-analysis`、`kunpeng_microarch`
- 通用热点代码手法（内联、分派、拷贝消除）：`hot-path-overhead-elimination`
- 分配路径成本与抖动：`allocation-path-optimization`
- 访存布局改写（AoS→SoA、分块、对齐）：`memory-access-optimization`
- 向量检索内部（索引/距离计算）：`vector-search-optimize`、`faiss-optimize`
- 引擎跨版本重放旧优化：`vector-db-optimization-migration`
- 服务端配置与内部状态（pagecache/锁等待/checkpoint）：`database-workload-analysis`
- Go 客户端 profiling 工具：`go-pprof-analysis`
- 系统库版本落后与"优化已存在"：`hot-library-version-gap`

## 输出契约

```
# <系统名> 记忆后端优化报告
## 1. 目标与口径        负载、绑核、缓存 md5、校验和、重跑波动
## 2. 三段归因          框架/驱动/服务端 前后绝对值与占比 + 拆分自证
## 3. 瓶颈模型          bottleneck_class / binding_constraint / 原始值 / 封闭轴
## 4. 杠杆清单与排序    ROI 表（预期 × 覆盖面 ÷ 风险）
## 5. 实施与验证        每轮改动、正确性、交错 A/B、采纳或回滚
## 6. 矩阵结果          8 组 × 2 阶段 × 2 架构
## 7. 负结果台账        手段 / 结果 / 根因 / 启示
## 8. 复测瓶颈与下一步
```

## 降级

| 场景 | 动作 |
|---|---|
| 无模型调用缓存 | 先 `record` 建库，期间数据不作结论 |
| 不能改客户端 | 只做服务端 + 运行配置；拆分改用驱动回调/中间件埋点 |
| 服务端封闭 | 跳过 `graph-store-readpath-patch`，预算投载荷与并发 |
| 无 top-down / PMU | `perf stat` 事件组近似，结论标低置信 |
| 只有一种架构 | 跨架构章节标"未测"，并写清待验证项 |
| 机器不独占 | 交错 A/B + 闲置度门控，只报相对差 |
