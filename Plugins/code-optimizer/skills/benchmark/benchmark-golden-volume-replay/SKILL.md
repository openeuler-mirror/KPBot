---
name: benchmark-golden-volume-replay
description: >
  mem0/milvus LOCOMO benchmark harness 的黄金卷 + clone 复用轮：build 轮把 Milvus 底库 finalize
  （flush + compaction）后 seal 成黄金卷，复用轮 probe 校验后 clone 卷并跳过 seed，保证多轮数据池逐字节一致；
  seal 用自动化命令 + 字段级报错诊断替代易错的手动步骤。
  适用：mem0/milvus 多轮基准要求每轮底库完全一致（数据池逐字节一致）、seed 慢想复用固化 Milvus 底库卷、
  手动 seal 常失效（忘 seal / 参数漂移 / stale assignments）需要自动化 + 可诊断化。
  不适用反例：单轮一次性基准（无复用需求）；非 mem0/milvus 负载（黄金卷/底库固化方式需重定）；
  或底库小到 seed 可忽略（clone + seal 的开销反而更大）。
---

# mem0/milvus 黄金卷 + clone 复用轮（标定案例）

## 何时使用

- 多轮对比要求底库一致：每轮重新 seed 会引入数据池漂移，而 golden volume 让复用轮 clone 同一固化卷。
- seed 耗时可观（本项目 seed + HNSW 索引构建 10–20 分钟），复用卷可把多轮成本压到 ingestion/qa。
- 手动 seal 反复出错，需要自动化 + 字段级诊断取代。

## 核心原则

1. **build 轮固化、复用轮 clone**：前缀切黄金前缀 → finalize（flush + compaction）→ seal 固化；
   复用轮 probe 就绪后 clone → 跳过 seed → run 结束删克隆卷（保证可重克隆）。
2. **数据池一致性是首要目标**：黄金卷让"9 个数据点数据池完全一致"成为可能（起点/终点行数 + seal 数据摘要实证，见 §3）。
3. **seal 要自动化 + 可诊断**：手动 seal 三类失效（忘 seal / 参数漂移 / stale assignments），用命令
   免手动组装参数 + 报错升级为 per-field diff。

## 步骤

### 1. build 轮：前缀切换 + finalize + seal

1. build 轮以 `MILVUS_GOLDEN_VOLUME_PREFIX` 前缀切黄金前缀，生成 compose。
2. seed 后 finalize（flush + compaction），把 HNSW 索引固化。
3. 容器停止后执行 `docker exec mem0-locomo-runner /workspace/mem0_locomo.sh seal-golden`（栈已下线、runner 容器仍在）；
   用 `seal-golden` 子命令免手动组装参数（分片形态自动合并 shard summaries，幂等）。

seal state 持久化：`seal-golden` **成功后**写 seal state 文件（`runtime/state/golden-seal.json`）作为黄金卷已固化标记
（finalize 只做 flush + compaction、不写 seal 标记），复用轮据此判断已有黄金卷、cleanup 据此判断跳过清理。
**CLI 来源与最小命令（seal / probe / clone 的成功判据）见 `references/golden-volume.md` §3.5**——本 skill 不随包
分发 benchmark bundle，拿不到时把该 bundle 列为外部必需输入。

**seal 时保存按稳定主键排序的数据摘要**（行数 + 每租户记录数分布 + 行内容/向量校验和），供组内与组间比对：
起止行数一致 ≠ 内容一致，摘要一致才判数据池一致（详见 references §5.5）。

### 2. 复用轮：probe → clone → 跳过 seed

```
probe 校验黄金卷就绪 → clone 卷 → 跳过 seed → 直接 ingestion/qa → run 结束删克隆卷
```

- probe 报错升级为 per-field diff + 无 marker 专属提示，避免"静默错配"。
- run 前 cleanup 检测到黄金 seal 状态已设则自动跳过（保护黄金卷）；未设则正常清理；
  run 结束 cleanup 删克隆卷保证多轮可重克隆。

### 3. 检验数据池一致性

复用轮起点/终点行数（如起点 81592 → 82480；口径 = `milvus-storage.json` 的 `row_count`，**8 用户 × 10000 seed 的
分组基线**：80000 seed + replay 动态写入 → build 轮 seal 时 81592、clone 轮 replay 后 82480；与 flatidx 标定的"20 万候选"是
20 用户配置，两套数据池勿混用）逐轮相同，`storage` 校验实证数据池一致，
从根上排除"收益来自少干活/少数据"。**但行数一致只证明数据量一致**，要判内容一致还需比对
seal 时保存的**稳定主键数据摘要**（行数 + 每租户记录数分布 + 行内容/向量校验和）：
组内 build 轮 vs clone 轮、组间 g1/g2/g3 逐项比对，摘要一致才判数据池一致。

## 门禁

- [ ] seal / probe / clone 三条命令按 references §3.5 的最小命令跑通，成功判据命中
- [ ] build 轮 finalize + seal 后，state 文件持久化
- [ ] 复用轮 probe→clone→跳过 seed 链路跑通
- [ ] 多轮起点/终点行数逐轮一致
- [ ] seal 数据摘要（稳定主键排序）组内/组间比对一致
- [ ] 独占分片形态下 clone 前校验分片归属；暂不兼容则用共享形态/单 runner
- [ ] probe 失败给 per-field diff 而非笼统报错
- [ ] run 结束删克隆卷，可重复克隆

## 结果（标定案例形态）

- 方案 C 统计设计（`benchmark-statistical-design`，独立 PR 待合入）依赖黄金卷：每组 build+seal 存档轮不计分，
  3 个 clone 复用轮为数据点；组间独立黄金前缀。
- 手动 seal 三类失效的报错从"查不出"变成 per-field diff 定位。

## 参考

- `references/golden-volume.md`：前缀切换/finalize/seal 完整流程、seal state 文件结构、
  三类手动失效根因、per-field diff 诊断、与统计设计/seed 稳定的衔接。