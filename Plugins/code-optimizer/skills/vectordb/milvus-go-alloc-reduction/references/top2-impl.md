# Go 检索合并分配削减实现与验证全量（标定案例 TOP2）

## 1. 证据（flatidx 后 qa 段）

- `typeutil.AppendFieldData` 累计分配 **20.6TB**（第一分配器）。
- `bytes.growSlice` **4.1TB**。
- `runtime.memmove` ~1.6%；`AppendFieldData` cum 5.69%。
- 驱动 GC 标记 + memmove。

根因：检索/retrieve 结果合并逐行调用 `AppendFieldData(dst, src, idx)`，`dst[i]` 首行初始化按字段类型分档
（v2.4.5 `pkg/util/typeutil/schema.go:587` 标量单元素 cap=1；`:714-720` 稠密 FloatVector `make([]float32, dim)`、
BinaryVector `make([]byte, dim/8)`），之后**容量不足时**（非每次 append）触发 growSlice 倍增 + 整段 memmove
搬运已累积元素。分配量最大的稠密向量字段从 **dim** 起倍增，不是从 1。

> **区分"逐行 append 次数"与"扩容次数"**：Go 切片容量充足时 append 复用底层数组、零分配，只有越界才扩容；
> 倍增扩容的实际扩容次数是 `O(log n)` 而非 `O(n)`。收益估算要用**目标调用栈的分配记录**——pprof 的
> `alloc_objects`/`alloc_space` 落在 `AppendFieldData`→`growSlice`→`memmove` 的量（标定案例 `AppendFieldData`
> 累计分配 20.6TB、`growSlice` 4.1TB），而不是"每行 append 都算一次整段复制"，否则高估可消除的分配。

## 2. 助手

`typeutil.PrepareResultFieldData(sample []*schemapb.FieldData, topK int64)`
（`pkg/util/typeutil/schema.go`），按 sample 字段类型/维度一次性预分配：标量 `make([]T,0,n)`、
稠密 `make([]float32,0,n*dim)`、稀疏 `make([][]byte,0,n)`，保持字段名/类型/FieldId/IsDynamic/Dim。

## 3. 改动清单（6 处裸 make→预分配 + 2 处容量修正）

### 3.1 裸 make → `PrepareResultFieldData`（3 文件 6 处）

| 文件 | 函数 | 容量 |
|---|---|---|
| querynodev2/segments/result.go | ReduceSearchResultData | topk*nq |
| 同上 | MergeInternalRetrieveResult | loopEnd |
| 同上 | MergeSegcoreRetrieveResults ×2 | len(selected) |
| proxy/task_query.go | reduceRetrieveResults | loopEnd |
| proxy/task_search.go | doRequery | GetSizeOfIDs(ids) |

### 3.2 容量修正 `limit → limit*nq`（1 文件 2 处，本已预分配）

| 文件 | 函数 | 原容量 | 新容量 |
|---|---|---|---|
| proxy/search_reduce_util.go | reduceSearchResultDataWithGroupBy | limit（=topk-offset，per-query） | limit*nq（总行上界） |
| 同上 | reduceSearchResultDataNoGroupBy | limit | limit*nq |

> 这两处 v2.4.5 里构造 `FieldsData` 已调用 `typeutil.PrepareResultFieldData`（search_reduce_util.go:91、:255），
> 不是裸 make；本补丁只把传参从 per-query 的 `limit` 上调为总行上界 `limit*nq`，属容量修正，不与"裸 make→预分配"
> 混为一谈。该文件唯一的 `make([]*schemapb.FieldData...)` 在 :413 `rankSearchResultData`（其后无逐行追加，不在清单内）。

未改：`internal/storage/mergeFloatVectorField`（已整段批量 append）、`FloatVectorFieldData.AppendRow`
（insert/import 路径，非检索合并）。

## 4. 五点等价性论证

1. **字段序/类型**：`PrepareResultFieldData(sample)` 按 sample 字段序构建 dst；`AppendFieldData` 以
   `src[i]→dst[i]` 同索引映射；所有 source 共用同一 schema 字段序 → 字段名/类型/FieldId 一一对应。
2. **nil/首行**：预分配后 `dst[i]` 非 nil 且带空 typed 切片，`dst[i]==nil || GetScalars()==nil` 恒假 →
   走 `else` 的 `append` 分支，等价"已 append 首行"的稳态路径（不改变首行初始化语义）。
3. **行序/去重/覆盖**：不改循环结构，`SelectMinPK`/`selectHighestScoreIndex`/`idSet`/`offset` 原样；
   `DeleteFieldData` 仍 `[:len-1]` reslice（预分配同样支持）→ 行序与去重逻辑逐行等价。
4. **Dim/稀疏**：稠密向量 outer Dim 两路径一致；稀疏向量 outer Dim 存在差异（`Prepare` 置 0，
   `Append` 首行置 `src.dim`），与已上线 proxy 侧 `search reduce` 行为一致——调用方本就按协议忽略稀疏
   outer Dim，见 §4.1。若调用方读取该字段，需在改前先核对协议约定，保留原维度或引用允许忽略的依据。
5. **容量上界 + 字节预算**：预分配容量均为总行数上界（`topk*nq` / `loopEnd` / `len(selected)` / `limit*nq`）≥ 实际
   append 数，仅可能轻微过分配（`maxOutputSize` 兜底）→ 不改变返回元素数量与内容。
   **但稠密向量按"行数上界 × 维度 × 4B"预分配，过滤结果很少时也会先申请整块内存**——分配前检查
   "行数上界 × 维度 × 字段宽度"乘积与字节预算上限，超预算的请求**保留按实际结果增长的路径**（或分块）。

### 4.1 稀疏 outer Dim 差异的核对口径

稀疏向量（SparseFloatVector）的 outer `Dim`：`PrepareResultFieldData` 预分配时置 0，逐行 `AppendFieldData`
首行会置 `src.dim`。两条路径对该字段赋值不同。当前调用方（proxy search reduce）不读取稀疏 outer Dim，
因此结果可观察等价；**若你的调用方读取该字段，需先确认协议是否允许忽略，否则保留原维度或改用
按实际结果增长的路径**。用带非零 Dim 的稀疏结果做一次序列化对比（改动前后逐字节）即可确认等价。

## 5. 构建与验证

**SVE 验证是条件项**：`whilelo`/`COMPILER_SUPPORTS_SVE` 是私有补丁 `0001-hnsw-scann-sve-pf` 才有的产物特征
（上游 knowhere v2.3.5——milvus 2.4.5 固定版、`internal/core/thirdparty/knowhere/CMakeLists.txt:15——的
`src/simd/distances_neon.cc` 662 行 SVE 行数为 0，ARM 守卫仅 `__ARM_NEON`）。**仅当在带 0001 补丁的标定构建树上
复现标定案例时才要求**；stock 2.4.5 / x86 部署手工落地按"单测 + 五点等价论证"验收，`sve-patch/` 不存在判
"未打补丁"而非"构建损坏"。

```bash
# （仅标定构建树）确保 SVE 启用
cd <build-tree> && mv sve-patch.bak sve-patch
bash server/build.sh
# 成功：SVE 验证通过：distances_neon.cc.o 含 SVE 指令（whilelo）
grep COMPILER_SUPPORTS_SVE milvus/cmake_build/CMakeCache.txt   # =1
objdump -d .../distances_neon.cc.o | grep -w whilelo            # 命中
```

**构建脚本来源与固定版本**：`server/build.sh` 位于 milvus 构建树
`modules/milvus/milvus-arm64-work-v2.4.5/server/build.sh`（milvus **2.4.5** 基线，离线大件只落盘不入 git；
从零重建 `cd modules/milvus/milvus-arm64-work-v2.4.5 && bash server/build.sh`）。本 skill 不随包分发该构建树，
拿不到时把 §3（6 处 + 2 处容量修正）改动手工落地到目标 Milvus 版本对应文件，并照 §4 五点等价 + 单测核验；不同版本
`internal/querynodev2`/`internal/proxy` 路径可能漂移，按实际版本定位。

单测（离线 `-tags dynamic`）：typeutil / querynodev2/segments / proxy 全 PASS。

## 6. 打包事故与修复

- top2-v1 = 裸 `milvus-offline` 直接打 tag，缺 etcd/minio/entrypoint → `RootCoord context deadline exceeded`，
  三轮 ~21s 零数据。
- top2-v2 = `FROM <sve-runtime-v1> + COPY bin/milvus`，2.35GB，冒烟正常。

## 7. 结果（3 轮中位）

| 指标（QA retrieval） | flatidx | TOP2 | 增益 |
|---|---|---|---|
| 吞吐 | 76.80 | 80.34 | +4.61% |
| p50 | 177.40 | 149.92 | -15.49% |
| p99 | 441.44 | 471.33 | +6.77%（~+30ms 回退） |
| qa elapsed | 300.18 | 286.885 | -4.43% |

请求计数 23420/23040 逐位一致。用户判定保留（中位/吞吐 > 尾延迟）。

## 8. 补丁

`modules/milvus/patches/0002-top2-go-alloc.patch`（88 行，仅 Go），**随 milvus 子仓 `manifest.yaml` 钉版本**
（milvus 2.4.5 基线）。应用前先 `git apply --check`。本 skill 只给机制与改动清单（6 处 + 2 处容量修正），
不复制整树源码；拿不到补丁时按 §3 手工落地并过 §4/§5 核验。