# 展平标量字段 + INVERTED 索引：实现与验证全量（标定案例）

## 1. 背景

基线 QA 瓶颈 = `metadata["user_id"]` 的 simdjson JSON 全量扫描（stage1 累计 ~46%），每请求在 20 万候选上
过滤。目标：把 user/agent/run 提为顶层标量字段 + INVERTED，候选集收窄到每用户 ~1 万。

## 2. 改动（mem0 `vector_stores/milvus.py`，4 处）

1. `create_col` 新建分支：`metadata` 后加 `FieldSchema(name="user_id", dtype=VARCHAR, max_length=512)` 等三个。
2. `index_params.add_index(field_name=..., index_type="INVERTED")` × 3（标定镜像内 vectors 为 HNSW M16/efC200，系 0001 补丁后状态；官方 mem0 2.0.14 该处为 AUTOINDEX 索引项，手工落地时把三条 INVERTED 追加在 vectors 索引项之后即可）。
3. `insert._build_record` 与 `update`：镜像三字段，缺失键 `""`（`(metadata or {}).get(...)`）。
4. `_create_filter`：`{user_id,agent_id,run_id}` → 顶层标量过滤；其余 key 维持 `metadata["key"]`；`"*"` 跳过；沿用转义。

纪律：新增分支在 `else`（新建 collection）内；既有 collection 走 `has_collection` 分支不变。
**注意**：既有 collection 是 3 字段 schema（仅 `id/vectors/metadata`），顶层三字段不存在——`_create_filter`
与 `insert/update` 都必须按实际 schema 走：有顶层三字段才用顶层标量过滤/镜像写入，否则回落到
`metadata["key"]` JSON 过滤；或先把旧 collection 按 6 字段重建并补齐三列（缺键 `""`）后再切换过滤器，
迁移未完成前不得把顶层过滤发给旧 collection。

补丁：`<repo>/modules/mem0/patches/0002-flatidx-top-level-fields.patch`；配套 `0001` 去 BM25。
**补丁来源/固定版本/基线**：本 skill 只给机制，具体 diff 在 mem0/milvus 联调项目的 mem0 子仓
（随 `manifest.yaml` 钉版本），适用基线 **mem0 2.0.14**（`mem0/vector_stores/milvus.py`）。使用前先
`git apply --check`；拿不到子仓时按 §2 四处改动手工落地，仍需过 §5 语义正确性核验。其它 mem0 版本
适配层路径/字段命名可能漂移，按版本差异手工移植。

## 3. 配套 seed 修复

schema 3→6 后，benchmark warmup seed 若用 pymilvus 直插仍写 3 列，会报 `expect 6 list, got 3`（pymilvus 2.4.5
严格列数校验）。对齐做法（内联自 `mem0-schema-aware-seed-insert`，独立 PR 待合入）：

```python
def _insert_batch(collection, ids, vectors, metadata):
    # 按 collection.schema 字段序组装各列，避免固定顺序导致字段对调写错
    name_to_col = {
        "id":       ids,
        "vectors":  vectors,
        "metadata": metadata,
        "user_id":  [m.get("user_id", "")  for m in metadata],
        "agent_id": [m.get("agent_id", "") for m in metadata],
        "run_id":   [m.get("run_id", "")   for m in metadata],
    }
    schema_names = [f.name for f in collection.schema.fields]
    unsupported  = [n for n in schema_names if n not in name_to_col]
    if unsupported:                     # 未支持字段组合：插入前报错而非静默写错
        raise ValueError(f"schema 含未支持字段 {unsupported}，请先在 name_to_col 补充映射")
    return collection.insert([name_to_col[n] for n in schema_names])
```

列序按 `collection.schema.fields` **反射组装**（不硬编码固定列顺序：`agent_id` 与 `run_id` 同为 VARCHAR，
schema 对调二者时固定顺序会把数据写进对方字段且不报错）；`name_to_col` 未覆盖的字段组合在插入前 `raise`。
长度与 `ids` 一致，缺失键写 `""`。

> **六字段是 fork 基线口径**：本案例 fork 的 `create_col` 基线是 3 字段 `[id, vectors, metadata]`，加三顶层字段后为
> 6 字段；若在 **stock mem0 2.0.14** 上手工落地（其 `create_col` 是 5 字段含 text/sparse BM25，见 §2 补丁说明），
> 同一做法得到的是 **8 字段（5+3）**，seed 侧 `name_to_col` 也须相应补 text/sparse 两列映射。runner 镜像
> `<runner-image:flatidx-seed-v1>`。

## 4. 镜像

- mem0：`<mem0-server-image:flatidx-v1>`（top-level 字段 + INVERTED + 既有 HNSW/L2 SVE/SQLite WAL）。
- 通用纪律：换表字段后新 tag 只加不覆盖；镜像 tag ↔ 基线 ↔ 补丁写入溯源表（溯源记账职责见 `opt-traceability-and-reverify`，独立 PR 待合入）。

## 5. 语义正确性核验

- 顶层 `user_id == "trace-user-001"` 计数 = 10000、空值 = 0 → 过滤打在完整底库。
- overall 23420 / qa 23040 与基线逐位一致 → 非假提速。
- **JSON vs 顶层过滤主键集合对照**（换过滤方式防漏/串租户）：同一批查询分别跑 JSON 过滤与顶层标量过滤，
  逐查询核对两者选中的**主键集合**是否一致，覆盖 `user_id`/`agent_id`/`run_id` 单键与**组合条件**
  （`user_id AND agent_id` 等），并逐条检查返回记录的**租户归属**。判定阈值：主键集合一致率 ≥ 99%、
  租户串组数 = 0。仅请求计数一致不够——请求数相同的两次回放可能返回不同租户的记忆。

## 6. 结果（3 轮中位）

| 指标（QA） | 基线 | flatidx | 增益 |
|---|---|---|---|
| retrieval 吞吐 | 27.47 req/s | 76.80 req/s | **+179.6%** |
| retrieval p50 | 605.51 ms | 177.40 ms | **-70.7%** |
| retrieval p99 | 772.58 ms | 441.44 ms | -42.9% |
| extraction 吞吐 | 4.76 | 13.31 req/s | **+179.6%** |
| 相位时长 | 838.72 s | 300.18 s | **-64.2%** |

## 7. 瓶颈迁移

- 基线：simdjson JSON 扫描 cum 45.88%（stage1 ~46%）。
- flatidx：JSON 扫描消失 → faiss `fvec_L2sqr_sve_pf` self ~52%。
- 后续 TOP2（Go 分配削减，见 `milvus-go-alloc-reduction`，独立 PR 待合入）在此基础上再 +4.61%。