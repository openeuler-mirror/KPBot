# seed schema 感知插入修复全量（标定案例）

## 1. 根因链

展平索引把 mem0 collection schema 由 3 字段（id/vectors/metadata）扩为 6 字段（+user_id/agent_id/run_id +
INVERTED）。benchmark warmup seed 不走 mem0，`seed_milvus_baseline.py` 用 pymilvus 直接
`collection.insert([ids, vectors, metadata])`（3 列），与 6 字段 schema 冲突 → pymilvus 2.4.5 严格列数校验报
`expect 6 list, got 3` → seed 相位崩溃（三轮无一进 replay）。

## 2. 改动（`runner/seed_milvus_baseline.py`）

新增辅助函数，替换两处插入，其余（batch 循环/版本/维度/hash）不动。

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
    if unsupported:                     # 未支持字段组合：插入前报错
        raise ValueError(f"schema 含未支持字段 {unsupported}，请先在 name_to_col 补充映射")
    return collection.insert([name_to_col[n] for n in schema_names])
```

- **列序按 `collection.schema.fields` 反射组装**：不写死 `[ids, vectors, metadata, user_ids, agent_ids, run_ids]`
  的固定顺序——`agent_id` 与 `run_id` 同为 VARCHAR，若 schema 对调二者，按固定顺序提交会把数据写进对方
  字段且不报错。字段名映射到值 → 再按 schema 字段序拼列，对调也能落对。
- 三列长度与 `ids` 一致，缺失键 `""`。
- 3/6 字段 schema 都兼容；`name_to_col` 未覆盖的字段组合在插入前 `raise`。

## 3. 验证

- seed 不再报 `expect 6 list, got 3`，`milvus-seed-summary.json` 行数正常（20×10000=20 万）。
- **按 schema 选过滤字段核验**（6 字段与 3 字段过滤字段不同）：6 字段 schema 用顶层
  `user_id == "trace-user-001"` 计数 = 10000、空值 = 0；3 字段 schema 里 `user_id` 在 `metadata` JSON 内，
  改测 `metadata["user_id"] == "trace-user-001"`，不测不存在的顶层字段。期望行数从本轮 seed 输入取得。

## 4. 配套关系

- 与展平/索引下推（collection schema 3 字段 → 6 字段）成对使用。六字段列序与类型：`id`（主键）、
  `vectors`（FLOAT_VECTOR）、`metadata`（JSON）、`user_id`（VARCHAR 512）、`agent_id`（VARCHAR 512）、
  `run_id`（VARCHAR 512）；三个顶层字段均建 INVERTED 索引。
- runner 镜像：`<runner-image:flatidx-seed-v1>`。

## 5. 通用化提示

任何"改了 A 侧数据接口、B 侧直插/旁路路径没跟上"都会以类似形式爆掉（列数不符 / 字段缺失 / 静默空集）。
对齐函数应基于**运行时 schema 反射**（`collection.schema.fields`）而非硬编码分支，才能同时兼容新旧两种 schema，
避免"改一处 schema 连带改多处硬编码版本"。这是"两侧一致性"原则（接口变更时同步所有写入路径）在本案例的落地。