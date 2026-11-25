---
name: mem0-flatidx-scalar-index
description: >
  记忆框架在向量库上的"过滤/索引下推"：把 metadata JSON 里的高频过滤键（user_id/agent_id/run_id）
  提为顶层 VARCHAR 标量字段并建 INVERTED 索引，过滤从"全库 JSON 扫描"改为"顶层标量过滤"，
  候选集收窄到每租户分片。以 mem0 + Milvus 为标定案例（QA 检索吞吐 +179.6%）。
  适用：mem0 等记忆框架在 Milvus/向量库上的过滤走 metadata JSON 全量扫描、候选集过大、检索吞吐低；
  需要把高频过滤键从 JSON 搬到原生标量索引；以及过滤键基数低、每租户分区性强（user/agent/session/run 等）的场景。
  不适用反例：没有框架→向量库过滤下推需求（纯后端引擎内部问题归 vectordb/）；过滤键基数接近行数
  （INVERTED 索引反而增加写入与内存开销）；或过滤键本就以原生列存储（那是换索引类型而非下推）。
---

# 过滤/索引下推：展平标量字段 + INVERTED 索引（标定案例：mem0 + Milvus）

## 何时使用

- 瓶颈分析显示后端热点是 `metadata["user_id"]` 这类 **JSON 全量扫描**（如 simdjson 累计 ~46%）。
- 每个检索请求要在全量候选（如 20 万）上做 JSON 过滤，候选集巨大。
- 过滤键（user/agent/session/run 等）基数低、每租户分区性强，值得从 JSON 搬到原生标量索引。

## 核心原则

1. **机制一句话**：把每请求对全量候选的 JSON 扫描，换成 N 个顶层 VARCHAR 字段 + INVERTED 索引的
   标量过滤，候选集收窄到每租户分片（标定案例：20 万 → ~1 万）。
2. **单文件、四处增量改动**，其余方法与既有 schema 分支逻辑保持不变。
3. **改了 schema 必须同步所有旁路写入路径**（seed 直插需按新 schema 写 6 列，做法见 `mem0-schema-aware-seed-insert`，独立 PR 待合入；`references/flatidx-impl.md` §3 已内联关键片段）。

## 步骤

### 1. 改框架的向量库适配层（mem0 为 `vector_stores/milvus.py`，四处）

**① `create_col` 新建分支**：在 `metadata` 后新增顶层标量字段：

```python
FieldSchema(name="user_id",  dtype=DataType.VARCHAR, max_length=512),
FieldSchema(name="agent_id", dtype=DataType.VARCHAR, max_length=512),
FieldSchema(name="run_id",   dtype=DataType.VARCHAR, max_length=512),
```

**② `index_params` 追加 INVERTED 索引**（标定镜像内 vectors 为 HNSW M16/efC200，系 0001 补丁后状态；官方
2.0.14 该处为 AUTOINDEX 索引项，手工落地时把三条 INVERTED 追加在 vectors 索引项之后即可）：

```python
index_params.add_index(field_name="user_id",  index_type="INVERTED")
index_params.add_index(field_name="agent_id", index_type="INVERTED")
index_params.add_index(field_name="run_id",   index_type="INVERTED")
```

**③ `insert` 的 `_build_record` 与 `update`（upsert）**：record/schema 镜像这三个字段，
缺失键写 `""`（`(metadata or {}).get(...)` 兜底 `None`）。

**④ `_create_filter`**：`{user_id, agent_id, run_id}` 三个 key 生成**顶层标量过滤**，其余 key 维持
`metadata["key"]` 兼容；`"*"` 通配照旧跳过；字符串转义沿用现有逻辑。

> 纪律：新增分支放在 `else`（新建 collection）内，既有 collection 走 `has_collection` 分支不受影响。
>
> **⚠️ 旧 collection 复用陷阱**：`has_collection` 分支保留的既有 collection 是 **3 字段 schema**（只有
> `id/vectors/metadata`），没有顶层 `user_id/agent_id/run_id`。若此时仍把 `_create_filter` 的
> `{user_id,agent_id,run_id}` 发给顶层字段，会**报字段不存在**或（更隐蔽地）**漏掉旧记录**。
> 必须二选一：
>
> 1. **读写路径都按实际 schema 走**：`_create_filter` 先探测 collection schema 是否有顶层三字段，
>    有 → 顶层标量过滤；无 → 回落到旧 `metadata["key"]` JSON 过滤（读路径）；`insert/update` 同样按
>    schema 字段组装（写路径），不要无条件镜像三字段。
> 2. **先迁移旧数据再切换过滤器**：把旧 collection 按 6 字段重建并 `insert` 补齐三列（缺失键 `""`），
>    逐条校验旧记录租户归属后再切换过滤器；迁移未完成前不得把顶层过滤发给旧 collection。

### 2. 同步旁路写入路径

benchmark warmup seed 若用 pymilvus 直插，schema 变 6 字段后必须写 6 列，否则
`expect 6 list, got 3` 崩溃 → 做法见 `references/flatidx-impl.md` §3（配套 skill `mem0-schema-aware-seed-insert` 独立 PR 待合入）。

### 3. 落库为单补丁 + 构建新 tag

补丁不随本 skill 包分发（只给可复现来源，不复制整树源码）。使用前先 `git apply --check` 验证：

```bash
git apply --check <repo>/modules/mem0/patches/0002-flatidx-top-level-fields.patch   # 先 dry-run
git apply <repo>/modules/mem0/patches/0002-flatidx-top-level-fields.patch
# 构建新 tag（不覆盖旧 tag）
```

**补丁来源与固定版本**（本 skill 只给机制，具体 diff 在 mem0/milvus 联调项目的 mem0 子仓，
随子仓 `manifest.yaml` 钉版本）：

- 补丁文件：`modules/mem0/patches/0002-flatidx-top-level-fields.patch`（配套 `0001` 去 BM25）。
- 适用基线：**mem0 2.0.14**（pip 包内路径 `mem0/vector_stores/milvus.py`）。
- 若拿不到该子仓，本 skill 的 §1 四处改动即完整语义，可直接在目标 mem0 版本的
  `vector_stores/milvus.py` 上手工落地；落地后仍须过 §4 的语义正确性核验。不同 mem0 版本的
  适配层路径/字段命名可能漂移，`git apply --check` 失败时按版本差异手工移植而非硬套。

### 4. 验证语义正确性 + 量化

- 顶层 `user_id == "trace-user-001"` 计数 = 10000、空值 = 0 → 过滤打在完整底库。
- 请求计数与基线逐位一致 → 非"少干活"。
- **JSON vs 顶层过滤主键集合对照**（防"换过滤方式仍漏/串租户"）：用**同一批查询**分别跑
  JSON 过滤（旧路径）与顶层标量过滤（新路径），对每个查询核对两者选中的**主键集合**是否一致，
  覆盖 `user_id`、`agent_id`、`run_id` 单键及**组合条件**（如 `user_id AND agent_id`），
  并逐条检查返回记录的**租户归属**。判定阈值：主键集合一致率 ≥ 99%、租户串组数 = 0。
  仅请求计数一致不够——两次回放请求数相同，返回的仍可能是不同租户的记忆。

## 门禁

- [ ] 四处改动齐全，既有 collection 走 `has_collection` 分支（读写路径都按实际 schema 选择，或先迁移旧数据再切换过滤器）
- [ ] seed 已同步写 6 列
- [ ] 补丁可 `git apply` 复现（或手工落地后过语义核验）
- [ ] 语义正确性核验通过（顶层计数=10000，空值=0）
- [ ] 请求计数与基线一致
- [ ] JSON vs 顶层过滤主键集合对照一致（覆盖 user_id/agent_id/run_id 及组合条件），租户串组数 = 0

## 结果（标定案例，3 轮中位，非收益承诺）

| 指标（QA 相位） | 基线 | flatidx | 增益 |
|---|---|---|---|
| retrieval 吞吐 | 27.47 req/s | 76.80 req/s | **+179.6%** |
| retrieval p50 | 605.51 ms | 177.40 ms | **-70.7%** |
| retrieval p99 | 772.58 ms | 441.44 ms | -42.9% |
| 相位时长 | 838.72 s | 300.18 s | **-64.2%** |

瓶颈迁移：simdjson JSON 扫描（cum ~46%）消失 → faiss L2 距离（self ~52%）浮现。

## 参考

- `references/flatidx-impl.md`：完整改动、过滤改写细节、语义正确性核验、瓶颈迁移证据、构建与镜像。
