# 热点迁移：优化只搬走瓶颈全量（标定案例）

## 1. simdjson 三项优化的教训

simdjson 三项优化（`bit_indexer::write` 标量化 / `-mcpu=native` / `skip_child` bit-trick）已编译进 milvus 镜像：
收益口径：**内核级 +14%（simdjson 内核自测）→ milvus 负载 +10.1%（端到端）**，两数分属不同测量层（不矛盾），
原始记录见 `docs/05-perf-analysis.md` §5.1（标定项目内部存档，本 skill 不再展开）。

- 在 base-qa profile 中**仍可见**：simdjson stage1 cum 仍 45.88%——即三项优化加速了 JSON 扫描内核，但**没有
  消除 JSON 扫描本身**（每行 `metadata["user_id"]` 仍全量 stage1）。三项优化**已落地、应标为完成**；
  但"已消化"不等于"扫描不再是开销"——每行仍全量 stage1 时，扫描（行数 × 每行成本）可能仍占主要开销。
  **下一步方向要用「同负载下每请求扫描耗时」判断**：每请求扫描耗时 = stage1 cum 占比 × 每请求墙钟耗时。
  若仍显著 → 方向是**减少扫描次数**（下推过滤/分层/剪枝，如后续 flatidx 所做的），不是继续抠内核单次成本；
  若已可忽略 → 才转向继续优化内核或换下一热点。
- **flatidx 后该整个路径过时**：flat-qa 中 simdjson 帧消失。继续优化 simdjson 之于当前部署（flatidx）
  **收益为零**。

## 2. 已过时的历史提议清单

| 提议 | 为什么 moot（对 flatidx 部署） |
|---|---|
| doc 缓存复用（`Json::at/exist` 每字段重 iterate） | mem0 检索仅按 `user_id` 单字段过滤；flatidx 已彻底移除 JSON 过滤 |
| 单行多字段合并（一次 iterate 多字段） | 当前无多字段 JSON 过滤 |
| SkipIndex 预过滤 | flatidx 已用标量 INVERTED bitmap 实现更彻底预过滤 |
| at_pointer 缓存 | 需改 simdjson 公共 API，且路径已过时 |

parser-cache 报告的 P0/P1（表达式内 document 复用 +57%、Json 级缓存）同属"JSON 过滤加速"家族，
对 flatidx 部署均已 moot。

## 3. 核心认知

1. **优化只搬走当前瓶颈**：加速 simdjson 内核 → flatidx 之后头号热点变成 `fvec_L2sqr_sve_pf`（SVE 距离）。
   瓶颈从"JSON 解析"迁移到"距离计算"。
2. **"仍可见" ≠ "还能挖"**：stage1 cum 45.88% 看起来还是大头，但它是"加速了内核、没消除扫描"，不是新可挖项。
   **但"已消化" ≠ "扫描已不再是开销"**：每行仍全量 stage1 时扫描本身可能仍是主要成本；用"每请求扫描耗时"
   （cum 占比 × 每请求墙钟）判断应"减少扫描次数"还是"继续优化内核"。本项目就是据此从"继续抠 simdjson 内核"
   转向"flatidx 下推过滤、减少扫描次数"。
3. **整条路径过时 = 收益为零**：下游换实现（flatidx 标量 bitmap）让旧路径消失，再优化旧路径没有意义。
4. **优化后必须重新 profile**：这是把"已消化"和"可挖"分开的唯一办法，旧热图会误导。

## 4. 与候选生成 / 溯源的衔接

- 候选生成（`opt-candidate-falsification`，独立 PR 待合入）：证伪优先第一步就是"这条路径在当前 profile 还成立吗"。
- 瓶颈挖掘（`opt-bottleneck-mining`，独立 PR 待合入）：加速后重 profile 定位新头号热点。
- 溯源（`opt-traceability-and-reverify`，独立 PR 待合入）：把"已消化/已过时"登记进溯源表，防止后面又去动 simdjson。

## 5. 坑点

- **看到高占比就继续挖同一内核**：可能已消化，真正的下一瓶颈在别处。
- **用旧 profile 规划新优化**：加速后权重结构变了，旧热图是过时信息。
- **忽略下游换实现**：flatidx 让整条 JSON 过滤路径 moot，继续优化是重复投入。