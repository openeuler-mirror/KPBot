---
name: mem0-schema-aware-seed-insert
description: >
  数据接口（collection schema）变更后，旁路写入路径的 schema 感知对齐：当记忆框架把 collection 从 3 字段
  扩为 6 字段后，benchmark warmup seed 直插仍写 3 列会报列数不符，用按 `collection.schema.fields` 自动选列数的
  辅助函数替换直插。以 mem0 + Milvus 为标定案例（修复 `expect 6 list, got 3`）。
  适用：做展平/索引下推等 schema 变更后 seed 相位崩溃、pymilvus 报列数不符（expect N list, got M）、
  或新增顶层字段后所有直插 collection 的写入路径需要同步对齐的场景。
  不适用反例：没改 collection schema；或写入路径不是 benchmark/seed 直插（走框架自身的 insert/upsert，
  那是框架适配层职责，本 skill 只覆盖旁路 seed 直插路径的对齐）。
---

# 旁路写入路径的 schema 感知对齐（标定案例：seed 直插）

## 何时使用

- 做了展平/索引下推（collection 3 字段 → 6 字段）后 seed 相位崩溃。
- pymilvus 报 `expect 6 list, got 3`。
- 任何"直插 collection"的写入路径在 schema 变更后需要对齐。

## 核心原则

1. **根因**：benchmark warmup seed 不走框架，`seed_milvus_baseline.py` 用 pymilvus 直接
   `collection.insert([ids, vectors, metadata])`（3 列），与 6 字段 schema 冲突，pymilvus 严格列数校验报错。
2. **改动最小化**：只加一个 schema 感知辅助函数并替换两处插入，batch 循环/版本/维度/hash 一律不动。
3. **两侧一致性**：改数据接口必须同步所有写入路径（本 skill 就是 B 侧对齐 A 侧的 schema 变更）。

## 步骤

### 1. 新增 `_insert_batch`（摘要）

核心是一个 schema 感知辅助函数：按 `collection.schema.fields` **反射**组装列序（字段名映射到值 → 再按 schema
字段序拼列），`name_to_col` 未覆盖的字段组合在插入前 `raise`（不静默写错），长度与 `ids` 一致、缺失键 `""`。
完整代码与逐条解释见 `references/seed-fix.md` §2。

### 2. 替换所有直插路径

把原 `collection.insert([ids, vectors, metadata])` 全部替换为 `_insert_batch(...)`。

### 3. 验证（摘要）

- 重跑 seed：不再报 `expect 6 list, got 3`，`milvus-seed-summary.json` 行数正常。
- 按 schema 选过滤字段核验、期望行数从本轮 seed 输入取得——完整判定与数字见 `references/seed-fix.md` §3。

## 门禁

- [ ] 辅助函数按 `collection.schema.fields` 反射组装列序，不硬编码固定列顺序
- [ ] 未支持字段组合在插入前 `raise`，不静默写错
- [ ] 所有直插路径均已替换
- [ ] 6 列 / 3 列两种 schema 均可跑通
- [ ] seed 后计数按 schema 选过滤字段核验（顶层或 `metadata` 内），期望行数取自本轮 seed 输入

## 产物

- `seed_milvus_baseline.py` 的 `_insert_batch`
- runner 镜像新 tag（标定案例 `<runner-image:flatidx-seed-v1>`）

## 参考

- `references/seed-fix.md`：完整根因链、改动 diff、与展平索引的配套关系、验证输出。