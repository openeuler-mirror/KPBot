---
name: milvus-go-alloc-reduction
description: >
  Milvus Go 侧检索/retrieve 结果合并的分配削减：用 `typeutil.PrepareResultFieldData` 按总行数上界一次性预分配，
  替换裸 make + 逐行 append 触发的 `bytes.growSlice` 倍增扩容（6 处裸 make→预分配 + 2 处容量修正，语义等价）。
  以 Milvus 2.4.5 为标定案例（QA 吞吐 +4.61% / p50 -15.49%，代价 p99 +6.77%）。
  适用：Go profile 里 `typeutil.AppendFieldData`/`bytes.growSlice`/`runtime.memmove` 分配 churn 占大头；
  检索/retrieve 结果合并是逐行 append、`dst[i]` 从首行容量（标量 cap=1、稠密向量 cap=dim）起反复倍增扩容；要复现"预分配替代逐行 append"。
  不适用反例：瓶颈在 C++ 距离内核或框架侧过滤（那是同域其它专项/相邻域的职责，本 skill 只覆盖 Go 侧结果合并）；
  或结果合并本已整段批量 append（无需此优化）。
---

# Milvus Go 检索合并分配削减（标定案例 TOP2）

## 何时使用

- Go profile 显示 `typeutil.AppendFieldData` 累计分配巨大（标定案例 20.6TB）、`bytes.growSlice` 数 TB、
  `runtime.memmove` 占比可观。
- 检索/retrieve 结果合并是逐行 `AppendFieldData`，`dst[i]` 从首行容量（标量 cap=1、稠密向量 cap=dim）起反复倍增扩容。
- 要复现"预分配替代逐行 append"的收益形态（吞吐 +4.61% / p50 -15.49%，并接受 p99 +6.77% 代价）。

## 核心原则

1. **复用自带助手** `typeutil.PrepareResultFieldData(sample, total)`（proxy 侧 search reduce 早已在用），
   按字段类型/维度一次性预分配容量。
2. **6 处裸 make→预分配（3 文件）+ 2 处容量修正（search_reduce_util.go 两处本已用 `PrepareResultFieldData`、
   只把 `limit→limit*nq`），全部 Go，语义等价**；不改循环结构/去重/覆盖逻辑。
3. **必须附五点语义等价性论证 + 单测**。

## 步骤

### 1. 定位热点

Go 堆 profile 看到 `AppendFieldData`（第一分配器）+ `bytes.growSlice`（倍增扩容）+ `runtime.memmove`。

根因：`typeutil.AppendFieldData(dst, src, idx)` 逐行调用，`dst[i]` 首行初始化按字段类型分档——标量/稀疏为单元素
切片（cap=1），稠密 FloatVector 为 `make([]float32, dim)`（cap=dim），BinaryVector 为 `make([]byte, dim/8)`；
随后**每次容量不足时**（不是每次 append）触发 `growSlice` 倍增扩容 + 整段 `memmove` 搬运已累积元素。
**分配量最大的稠密向量字段从 dim 起倍增，不是从 1**——触发画像与收益直觉据此分档，不能一律说"cap=1 起倍增"。
**要区分"逐行 append 次数"与"扩容次数"**：Go 切片容量充足时 append 复用底层数组、零分配，只有越界才扩容；
倍增扩容下实际扩容次数是 `O(log n)`，不是 `O(n)`。收益应按**目标调用栈的分配记录**估算
（pprof `alloc_objects`/`alloc_space` 落在 `AppendFieldData`→`growSlice` 的量），而不是"每行 append 都算一次
整段复制"，否则会高估可消除的分配。

### 2. 把裸 make 换成预分配

**6 处裸 `make([]*schemapb.FieldData, len(...))` → `typeutil.PrepareResultFieldData(sample, total)`**，分布在 3 文件：
`internal/querynodev2/segments/result.go`（4 处）、`internal/proxy/task_query.go`（1 处）、`internal/proxy/task_search.go`
（1 处）；另有 **2 处容量修正 `limit → limit*nq`** 在 `internal/proxy/search_reduce_util.go`（该两处本已用
`PrepareResultFieldData`、非裸 make，只是把 per-query 的 `limit` 上调为总行上界 `limit*nq`）。完整
文件/函数/容量清单见 `references/top2-impl.md` §3。

模式：`make([]*schemapb.FieldData, len(...))` → `typeutil.PrepareResultFieldData(sample, total)`。

### 3. 写五点等价性论证

1. 字段序/类型：按 sample 字段序构建，`src[i]→dst[i]` 同索引映射。
2. nil/首行：预分配后 `dst[i]` 非 nil，走 `else` 的 append 分支，等价"已 append 首行"稳态。
3. 行序/去重/覆盖：不改循环与 `SelectMinPK`/`idSet`/`offset`，逐位不变。
4. Dim/稀疏：稠密 dim 两路径一致；稀疏 outer dim 差异与已上线 proxy 行为一致（核对口径见 reference §4.1）。
5. **容量上界 + 字节预算**：预分配容量均为总行数上界（`topk*nq`/`loopEnd`/`len(selected)`/`limit*nq`）≥ 实际
   append 数。**但稠密向量按"行数上界 × 维度 × 4B"预分配，过滤结果很少时也会先申请整块内存**——分配前先
   检查"行数上界 × 维度 × 字段宽度"的乘积，与**字节预算上限**比较，超预算的请求**保留按实际结果增长的路径**
   （或分块），不盲目整块预分配。

### 4. 单测 + 重建

命中被改函数的既有单测（离线 `-tags dynamic`）：typeutil / querynodev2/segments / proxy 全 PASS。
重建命令与 SVE 验证见 `references/top2-impl.md` §5。

**SVE 验证是条件项，不是本 skill（纯 Go 分配改动）的无条件门禁**：`whilelo`/`COMPILER_SUPPORTS_SVE` 是私有
补丁 `0001-hnsw-scann-sve-pf` 才有的产物特征（上游 knowhere v2.3.5 的 `distances_neon.cc` SVE 行数为 0，仅
`__ARM_NEON` 守卫）。**仅当在带 0001 补丁的标定构建树上复现标定案例时才要求 SVE 验证**；在 stock 2.4.5 / 任何
x86 部署手工落地时，Go 改动按"单测 + 五点等价论证"验收即可（`sve-patch/` 目录不存在属正常，判"未打补丁"而非"构建损坏"）。

**构建脚本来源与固定版本**：`server/build.sh` 位于 milvus 构建树
`modules/milvus/milvus-arm64-work-v2.4.5/server/build.sh`（milvus 2.4.5 基线，离线大件只落盘不入 git）。
补丁 `modules/milvus/patches/0002-top2-go-alloc.patch` 随 milvus 子仓 `manifest.yaml` 钉版本。拿不到该子仓时，
本 skill 的 §2（6 处 + 2 处容量修正）即完整语义，可在目标 Milvus 版本对应文件上手工落地；不同版本适配层路径可能漂移。

### 5. 打包（只换二进制、复用基座）

```dockerfile
FROM <milvus-image:sve-runtime-v1>
COPY <top2构建树>/bin/milvus /milvus/bin/milvus
```

→ 新 tag `<milvus-image:top2-v2>`（**不要**把裸 `milvus-offline` 直接打 tag，会缺 etcd/minio/entrypoint 启动即崩）。

## 门禁

- [ ] 6 处裸 make 均已换 `PrepareResultFieldData`、2 处容量修正 `limit→limit*nq` 已落实，容量为总行数上界
- [ ] 预分配前检查"行数上界 × 维度 × 字段宽度"乘积与字节预算，超预算保留按实际结果增长的路径
- [ ] 收益估算用目标调用栈分配记录（alloc_objects/alloc_space），未把"逐行 append"误当"逐行扩容"
- [ ] 五点等价论证书面完成（含稀疏 outer Dim 核对口径）
- [ ] 单测（typeutil / querynodev2/segments / proxy）通过
- [ ] **（仅标定案例复现时）** SVE 验证通过（`whilelo` 命中）；stock 2.4.5 / x86 部署手工落地不要求此项
- [ ] **（仅部署镜像时）** 镜像是"复用基座 + 只换二进制"
- [ ] **（仅部署镜像时）** 新 tag 未覆盖旧 tag，冒烟通过

## 结果（标定案例，3 轮中位 vs flatidx 基线）

QA retrieval：吞吐 **76.80 → 80.34 req/s（+4.61%）**、p50 **177.40 → 149.92 ms（-15.49%）**，
代价 p99 +6.77%（~+30ms 尾延迟回退）。请求计数 23420/23040 逐位一致。用户判定保留（中位/吞吐 > 尾延迟）。
完整逐项表见 `references/top2-impl.md` §7。

## 参考

- `references/top2-impl.md`：完整补丁结构、五点论证全文、裸镜像事故与修复、SVE 验证命令、单测清单。