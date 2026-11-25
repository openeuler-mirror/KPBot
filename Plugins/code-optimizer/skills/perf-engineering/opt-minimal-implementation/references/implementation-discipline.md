# 实现纪律：两个真实案例

## 一、flatidx（Route B）：单文件四处改动

文件：`mem0/vector_stores/milvus.py`（pip mem0==2.0.14 包内路径）
补丁：`modules/mem0/patches/0002-flatidx-top-level-fields.patch`

> **基线字段口径（重要）**：本案例的 fork 基线已**剥离/条件化 text + sparse BM25 字段**（BM25 需 Milvus 2.5+，
> 与标定环境 Milvus 2.4.5 不兼容，原样会先在建 collection 处被 BM25 Function 挡住）。**上游 mem0 2.0.14 原样的
> `create_col` 新建分支是 5 字段**（id/vectors/metadata + text + sparse，并挂 BM25 Function），不是 3 字段——
> 因此"在 metadata 后加三字段"在本 fork 上得到 6 字段（3+3），在 stock 2.0.14 上得到 8 字段（5+3）。
> 案例数字 `expect 6 list, got 3` 是真实事故记录（fork 基线 3 字段、目标 6 字段），无需改动。

四处增量：
1. `create_col` 新建分支：`fields` 在 `metadata` 后加 `user_id/agent_id/run_id`（VARCHAR 512）。
2. `index_params` 追加三个 INVERTED 索引。
3. `insert` 的 `_build_record`：record 加镜像字段（缺失写 `""`，`(metadata or {}).get(...)` 兜底 None）。
4. `update`（upsert）：schema 同样镜像三字段；`_create_filter` 三 key 生成顶层标量过滤，其余维持 `metadata["key"]`。

纪律：新增分支放 `else`（新建 collection）内，既有 collection 走 `has_collection` 分支不受影响；单个补丁、无整树源码副本。

**⚠️ 既有集合的读写路径陷阱**：`has_collection` 分支保留的既有 collection 是 3 字段 schema（id/vectors/metadata），
没有顶层 `user_id/agent_id/run_id`。若不止 `_create_filter` 把 `{user_id,agent_id,run_id}` 切到顶层字段，旧数据会
因新过滤器查不到（或报字段不存在）。必须**按实际 schema 选择读写路径**（有顶层三字段才用顶层过滤/镜像写入，否则
回落 `metadata["key"]` JSON 过滤），或**先迁移并按 6 字段回填三列**再切过滤器；并**用同一查询核对切换前后结果一致**
（主键集合一致率 ≥ 99%、租户串组数 = 0）。

## 二、TOP2：4 文件 8 处，最小变量面

文件：
- `internal/querynodev2/segments/result.go`：`ReduceSearchResultData`(topk*nq)、`MergeInternalRetrieveResult`(loopEnd)、`MergeSegcoreRetrieveResults`×2(len(selected))
- `internal/proxy/task_query.go`：`reduceRetrieveResults`(loopEnd)
- `internal/proxy/task_search.go`：`doRequery`(GetSizeOfIDs(ids))
- `internal/proxy/search_reduce_util.go`：`reduceSearchResultDataWithGroupBy`(limit*nq)、`reduceSearchResultDataNoGroupBy`(limit*nq)

## 三、五点语义等价性论证（TOP2）

1. **字段序/类型**：`PrepareResultFieldData(sample)` 按 sample 字段序构建 dst；`AppendFieldData` 以 `src[i]→dst[i]` 同索引映射；所有 source 共用同一 schema 字段序。
2. **nil/首行**：预分配后 `dst[i]` 非 nil 且带空 typed 切片，`dst[i]==nil || GetScalars()==nil` 恒假 → 走 `else` 的 `append` 分支，等价"已 append 首行"稳态。
3. **行序/去重/覆盖**：不改循环结构、`SelectMinPK`/`selectHighestScoreIndex`/`idSet`/`offset`；`DeleteFieldData` 仍 `[:len-1]` reslice（预分配同样支持）。
4. **Dim/稀疏**：稠密 outer Dim 两路径一致；稀疏 outer Dim 存在差异（Prepare 置 0、Append 首行置 src.dim）。
   **这不能靠"其他 proxy 路径也返回 0"就判等价**——原路径返回 src.dim、预分配路径返回 0，调用方收到的字段值
   变了，只有"其他路径同样返回 0"证明不了"当前调用方结果等价"。必须二选一：**保留原维度**（让预分配路径也返回
   src.dim），或**引用允许忽略该字段的协议约定**（指出下游不消费该 dim 字段）；并**比对非零 dim 稀疏结果的前后
   序列化输出**，确认无差异。
5. **容量上界 + 字节预算**：预分配容量均为总行数上界（`topk*nq`/`loopEnd`/`len(selected)`/`limit*nq`）≥ 实际
   append 数，仅可能轻微过分配（`maxOutputSize` 兜底）。**但上界很大、实际只返回少量行时，预分配会在输出大小
   检查之前先占整块内存**——开工前按"字段宽度 × 向量维度 × 行数上界"估算字节，设**字节预算上限**，超预算走
   "按实际结果增长"路径或分块；并验证"大上界、小结果集"（如 topk=500、nq=100，过滤后每查询只回 2 行）场景。

单测：`pkg/util/typeutil`（TestAppendFieldData/TestMergeFieldData）、`internal/querynodev2/segments`（MergeSegcoreRetrieveResults 全分支）、`internal/proxy`（reduceSearchResultData/GroupBy/Requery）全 PASS。离线需 `-tags dynamic`（vendored librdkafka x86-64 链接问题）。

## 四、两侧一致性事故（seed）

schema 3→6 字段后，seed 直插 3 列 → pymilvus `expect 6 list, got 3` → seed 相位崩溃（三轮无一进 replay）。修复见 `mem0-schema-aware-seed-insert`。

## 五、镜像打包事故（top2-v1 → top2-v2）

| | 基线 runtime-v1 | 错误 top2-v1 |
|---|---|---|
| ENTRYPOINT | `/tini -- /usr/local/bin/entrypoint.sh` | `/tini --`（裸） |
| entrypoint/healthcheck/etcd/minio | ✅✅✅✅ | ❌❌❌❌ |

裸 `milvus run standalone` 缺 etcd/minio → `RootCoord starts error="context deadline exceeded"`，三轮均在 ~21s 内结束、零有效数据。

修复：`FROM <milvus-image:sve-runtime-v1>` + `COPY <top2构建树>/bin/milvus /milvus/bin/milvus` → top2-v2（2.35GB，<image-id>），冒烟三进程正常。

## 六、构建开关坑
- SVE 启用机制：`build.sh` 检测 `sve-patch/patches` 目录存在即启用；`/root` 树里曾改名 `sve-patch.bak` 禁用 → 必须 `mv sve-patch.bak sve-patch` 否则回退 NEON、丢失 `fvec_L2sqr_sve_pf`。
- `build.sh` 的 SVE 验证 `objdump | grep -qw whilelo` 在 `set -o pipefail` 下 grep -q 提前退出致 objdump SIGPIPE(141) → 验证恒假失败；改为 `grep -w whilelo >/dev/null`。
