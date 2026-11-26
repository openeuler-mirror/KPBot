# 标定案例 2：Mem0 + Milvus（鲲鹏 aarch64）

> 供 `agent-memory-db-optimization` 在阶段 4/5 按需 Read：与 Graphiti + Neo4j（标定案例 1，
> 图库 + Bolt 协议）互补，这是**向量库后端 + 框架侧过滤/索引**形态的标定案例。
> 数字是"收益分布形态"，不是对其它部署的承诺。项目：mem0 2.0.14（Python 框架）+ Milvus 2.4.5
> （Go 服务 + C++ knowhere/faiss 内核），LoCoMo trace-stress-replay 负载，20 用户 / 20 并发，
> 每用户预热 10000 条（共 20 万条 1024 维向量），LLM/embedding 全部离线 mock 回放。
>
> **标定镜像含两处未上游补丁**：① 剥离 `text`/`sparse` 的 BM25 schema——官方 mem0 2.0.14 的
> `create_col` 无条件建 `[id, vectors, metadata, text, sparse]` 5 字段 + `FunctionType.BM25` +
> `enable_analyzer`（docstring 自述"Milvus 2.5+ supports native BM25"），而 Milvus 2.4.5 全源码无
> BM25/Function/analyzer 支持，标定镜像已把 `create_col` 新建分支剥离为 3 字段基线并另建 seed；
> ② SVE 距离内核 `fvec_L2sqr_sve_pf`——官方 knowhere v2.3.5 该处为 `fvec_L2sqr_neon`（仅 NEON）。
> 按官方 2.0.14 + 2.4.5 裸复现会先在 `create_collection` 撞 BM25 Function 不被支持，绕过后距离
> 内核也只是 NEON 而非 SVE。

## 1. 三段归因的对应形态

本案例不是"框架 / 驱动解码 / 服务端"三段，而是**"框架等待 / 服务端向量检索 / 服务端 Go 合并"**三段：

- **框架层（mem0，Python）**：81% 叶级时间停在 `_blocking`（等 gRPC 返回），真实业务 CPU <3%
  → 是**等待者**，不是被优化对象。
- **服务端向量检索（Milvus C++ knowhere）**：优化前热点是 `metadata["user_id"]` 的 **simdjson JSON
  全量扫描**（stage1 累计 ~46%，每请求在 20 万候选上做 JSON 过滤）；优化后迁移为 **faiss L2
  距离内核** `fvec_L2sqr_sve_pf`（self ~52%；标定镜像 SVE 补丁，官方 knowhere v2.3.5 为 `fvec_L2sqr_neon`）。
- **服务端 Go 合并（Milvus proxy/querynode）**：`typeutil.AppendFieldData` 累计分配 20.6TB +
  `bytes.growSlice` 倍增扩容。

三层结论印证了枢纽 SKILL.md 的核心信条：**CPU 花在"搬运与解码/过滤"而不是"算法"**，且
**瓶颈会迁移**（JSON 扫描 → 向量距离）。

## 2. 两项保留的杠杆

### L8 过滤/索引下推（flatidx）：QA 检索吞吐 +179.6%

把 mem0 放在 `metadata` JSON 里的三个高频过滤键（`user_id` / `agent_id` / `run_id`）提为
collection 顶层标量字段 + INVERTED 索引，过滤从"20 万候选 JSON 扫描"收窄到"每租户 ~1 万
候选向量检索"。改动集中在 `mem0/vector_stores/milvus.py` 的 4 处：① `create_col` ②
`index_params` ③ `insert._build_record` 与 `update`（共用同一 record 镜像）④ `_create_filter`，
新增分支放 `else`（新建 collection）内，既有 collection 走 `has_collection` 分支不受影响。
（本案例 fork 的 `create_col` 基线是 3 字段 `[id, vectors, metadata]`，加 3 列后为 6 字段；官方
mem0 2.0.14 的 `create_col` 是 5 字段含 BM25，见文件首部"标定镜像"注，同样做法在官方包上是 8 字段。）

| 指标（QA 相位，3 轮中位） | 基线 | flatidx | 增益 |
|---|---|---|---|
| retrieval 吞吐 | 27.47 req/s | 76.80 req/s | **+179.6%** |
| retrieval p50 | 605.51 ms | 177.40 ms | **-70.7%** |
| retrieval p99 | 772.58 ms | 441.44 ms | -42.9% |
| extraction 吞吐 | 4.76 req/s | 13.31 req/s | **+179.6%** |
| 相位时长 | 838.72 s | 300.18 s | **-64.2%** |

### Go 分配削减（TOP2）：再 +4.61%（代价 p99 +6.77%）

用 `typeutil.PrepareResultFieldData(sample, total)` 按总行数上界一次性预分配，替换裸
`make([]*schemapb.FieldData, len(...))` + 逐行 append（触发 `bytes.growSlice` 倍增 + 整段 memmove），
4 文件 8 处，全部 Go、语义等价（五点论证 + 单测）。

| 指标（QA retrieval） | flatidx | TOP2 | 增益 |
|---|---|---|---|
| 吞吐 | 76.80 | 80.34 req/s | **+4.61%** |
| p50 | 177.40 | 149.92 ms | **-15.49%** |
| p99 | 441.44 | 471.33 ms | +6.77%（~+30ms 尾延迟回退） |

保留决策：中位/吞吐改善价值 > 尾延迟代价。**请求计数 23420（overall）/ 23040（qa）逐位一致**只是第一层
铁证（排除"少干活"）；第二层是**语义正确性核验**：用**同一批回放查询**逐条核对 returned 记录的
租户归属、返回条数、top-k 主键（或召回率），判定阈值如"同查询 top-k 主键集合一致率 ≥ 99%、租户
归属串组数为 0"。只靠请求总数 + 顶层过滤计数等于 10000（`user_id == "trace-user-001"`）不够——过滤器
漏选中另一租户时，请求总数与单租户行数仍可能不变；必须回到逐查询级别核对"返回了谁、返回了几条"。

## 3. 证伪的方向（详见 negative-results.md 补充）

- **TOP3 实体 boost 削减**：`grep -c` 于采样栈零命中 → **采样中未观察到**该分支执行（短调用可能完整落在
  两次栈采样之间，不能据此断言"零触发"；要确证需统计同一回放中的分支进入次数，或从完整输入验证实体提取
  始终为空），放弃（零成本证伪）。
- **TOP1(c) 查询内并行化**：算力利用率 ~8%（~2.7 GFLOP/s/核 vs ~36 GFLOP/s 峰值，每查询 ~41MB
  DRAM 流量）→ 把"带宽受限"列为**待验证假设**（~8% 与 41MB 均不能证明 DRAM 已饱和；需对照相同
  绑核与并发下的实测带宽与平台可持续带宽，或单独报告并行实验的吞吐变化），2 核已被查询级并行占满 → 并行不涨吞吐。
- **TOP1(b) 双累加器 + 预取 0x400**：1 轮无改善，且 `libknowhere.so` 206MB vs 314MB（`.text`
  +45%）confound → 无法干净归因，放弃。
- **GOMEMLIMIT 注入**：吞吐无变化，仅说明**本次软限设置未带来可辨别收益**——live heap 小不等于无 GC
  成本（频繁分配仍可能让并发标记与 GC assist 占 CPU）；要排除 GC 瓶颈需补同一 QA 窗口的 GC CPU 开销，
  否则结论限定为"本次调参结果"。

## 4. 与本案例对应的原子 skill

本案例的可复用实现已拆成独立原子 skill（随独立 PR 逐个合入，合入前按名称待落地引用，落地后平铺引用）：

| 环节 | skill |
|---|---|
| 过滤/索引下推（L8） | `mem0-flatidx-scalar-index`（独立 PR 待合入） |
| 数据接口变更后写入路径对齐 | `mem0-schema-aware-seed-insert`（独立 PR 待合入） |
| Go 检索结果合并分配削减 | `milvus-go-alloc-reduction`（独立 PR 待合入） |
| SVE 距离内核确认 / 带宽受限判定 | `milvus-sve-hnsw-kernel`（独立 PR 待合入） |
| 基准运行与解读 | `locomo-benchmark-replay`（独立 PR 待合入） |
| 测量方法论七阶段 | `perf-engineering/` 域的 `opt-*` 七个 skill（独立 PR 待合入） |

> 这些 skill 尚未随本仓库合入时，其实现要点已内联在 `references/levers.md`（L8 小节含
> `create_col` / `index_params` / `insert` / `update` / `_create_filter` 的具体做法），
> 可作为不依赖外部 skill 的自足实现入口。

## 5. 与标定案例 1 的差异（方法论适用性）

| 维度 | Graphiti + Neo4j（案例 1） | Mem0 + Milvus（案例 2） |
|---|---|---|
| 后端 | 图库，Bolt/PackStream | 向量库，gRPC + C++ 内核 |
| 客户端运行时 | Python（解释型）→ 解码/水合下沉收益大 | Python（解释型）但框架只是等待者，下沉无意义 |
| 主杠杆 | L1 载荷 → L2 解码下沉 → L3 水合下沉 → L4 多进程 | **L8 过滤/索引下推** → Go 分配削减；L2/L3 不适用 |
| 服务端可改性 | 服务端补丁收益随架构/并发反转 | SVE 内核已生效（标定镜像；带宽受限为待验证假设），并行无效 |

结论：枢纽 SKILL.md 的「通用性边界」三问在本案例得到印证——**先判客户端运行时与序列化边界，
再判服务端可改性**；向量库形态的杠杆（过滤下推、索引、Go 分配）与图库形态（解码、水合、投影）
完全不同，但"冻结负载 → 三段归因 → 按证据选杠杆 → 单变量验证"的链路不变。
