# 证伪案例全量记录

## 案例 A：TOP3 实体 boost 零触发（证伪）
**假设**：`_compute_entity_boosts` 是被 `_blocking` 掩盖的隐性第二条 milvus 检索链路（最多 8 次 `top_k=500`）。

**证据**：
1. `grep -c "_compute_entity_boosts\|entity_store.search\|retrieve.entity_boost"` 于 flat-qa 的 4 个 `pystack_*.json` → **全部 0 次命中**。这只能记为"**采样未观察到**"——短调用可能落在相邻两次采样之间被漏掉；故继续用下面 2/3/4 的调用计数与控制流证明"零触发"。
2. `keyword_search()`（`mem0/vector_stores/milvus.py:236-256`）：`if not self._has_bm25_schema: return None`；flatidx 无 BM25 字段 → 直接返回 None，不发第二次请求（可证明分支不可达）。
3. `_compute_entity_boosts` 调用点（`mem0/memory/main.py`，2.0.14 口径门控在 :1633-1635）：仅当 `extract_entities(query)` 非空才进入；LOCOMO 检索 query 未触发实体提取（控制流证明入口条件恒假）。
4. `_search_vector_store`（`main.py`，2.0.14 口径向量检索调用在 :1611-1615）实际热路径 = `vector_store.search(top_k=max(limit*4,60))` 一次调用（完整请求轨迹里只有一次 milvus 检索）。

**结论**：每个 query 实际只发 1 次 milvus 请求，实体 boost 零触发（采样 + 控制流 + 轨迹三重确认，非仅 grep 零命中）→ 无测对象，放弃。省掉一次 mem0 Python 镜像改造 + 重建 + 全量对比。

## 案例 B：TOP1(c) 距离并行化（带宽受限证伪）
**假设**：查询内并行能提速 1.5–3 倍。

**证据**：
1. `fvec_L2sqr_sve_pf`（`distances_neon.cc:93-110`）：SVE 256-bit（`svcntw()`=8，`sve_default_vector_length=32`），预取 `0x200=512B`，单累加器。
   （来源：该内核出自私有 SVE 补丁，上游 knowhere v2.3.5 仅 NEON，stock 树检索不到此符号，行号仅补丁树内有效。）
2. 算力利用率 ~2.7 GFLOP/s/核 ≈ 峰值 ~36 的 **~8%**；每查询 ~41MB DRAM 是**按向量大小推算**、非实测。
   **低利用率不能单独判带宽受限**——指令依赖 / 访存延迟 / 单累加器依赖链同样能产生 ~8%。判"带宽受限、并行无效"
   需补：**实测同一窗口 DRAM 带宽 vs 平台可持续带宽**，实测贴近可持续带宽才成立；缺实测时标"待验证假设"。
3. 查询级并行已存在：`hnsw.cc:201/295/343` 每个 query 的 `searchKnn` 进全局 folly ThreadPool；线程池=2（`querynodev2/server.go:186-187` → `GetCPUNum()×factor`，`GetCPUNum()=GOMAXPROCS=2`）→ 2 核已占满。

**结论**：核已占满（这点已实证）；带宽受限为**待验证假设**（缺实测带宽对照）→ 再切线程只增开销，放弃。
正确方向（降访存流量）会动召回率，超出范围。

## 案例 C：GOMEMLIMIT 从未触发（资源峰值远低阈值）
**步骤**：注入 `GOMEMLIMIT=6GiB` + `GODEBUG=gctrace=1`（注入已核实生效）。

**证据**：
- Go 堆 live 峰值仅 **637 MB**，next-GC goal 峰值 **1.9 GB**，远 < 6GiB soft limit。
  **但这不足以证明"运行时总内存远低于 6GiB"**：live heap 与 next-GC goal 都低时，栈、其它运行时结构等
  仍可能让**总量接近上限**。核对口径用同窗口 **`/memory/classes/total:bytes` −
  `/memory/classes/heap/released:bytes`**（= 运行时当前管理的总内存，含栈/元数据/GC 结构），
  与 6GiB 限额比，而不是只看堆 live。
- GOGC 被 gc_tuner 钉死 **200**（`gc_tuner.go`：按宿主总内存×0.9 算阈值，本机 749G→674G 恒 > 已用 → clamp 到 maxGOGC=200）。
- STW 停顿 p99 = 5.95ms。
- 1 计时轮 qa retrieval 吞吐 -5.2%（72.84 vs 76.80），p50 +10.9%、p99 +2.7% → 方向为负且落在轮间噪声内。

**结论**：GC 不是瓶颈、GOMEMLIMIT 从未生效的结论**限定于本次限额实验**（本轮 6GiB 配置下未观察到收益）；
要彻底排除 GC 瓶颈还需同窗口的 GC CPU 与停顿数据。放弃整条 Go GC 调优路线，环境已还原。

**前置调研要点**：
- `GOGC` env 无效（被 gc_tuner 覆盖）；`GOMEMLIMIT` 有效（调参器只碰 SetGCPercent）；`GODEBUG=gctrace=1` 生效。
- 注入点：`mem0_locomo.sh` 是宿主 bind-mount 单文件，改宿主即生效优先级最高。

## 案例 D：TOP1(b) 无增益 + 无法干净归因（无增益）
**改动**（`distances_neon.cc` `fvec_L2sqr_sve_pf` 93–131；该内核出自私有 SVE 补丁，上游 knowhere v2.3.5 仅 NEON，
stock 树检索不到此符号，行号仅补丁树内有效）：
| 项 | 原 | top1-b |
|---|---|---|
| 累加器 | 单 sum | 双 sum0/sum1 |
| 主循环步长 | `svcntw()` | `2*svcntw()` |
| 预取距离 | 0x200 (512B) | 0x400 (1024B) |
| 尾部 | 无 | `svwhilelt_b32` 掩码 |

**结果**：1 轮 p50 表观 -19.8%（口径：对 top2 三轮 p50 中位 149.92ms，记录见 `opt-quantify-and-decide`，
独立 PR 待合入），但 top2 自身 run#1 p50=120.93ms ≈ top1b 120.21ms → 实为轮间方差；p95/p99 落在更差一端，吞吐持平。

**混淆**：`libknowhere.so` 206MB vs 314MB，`.text` +45% → 除内核外还有别的东西变了，无法干净归因。

**结论**：放弃。两条硬道理：①单轮对比危险（看多轮分布）；②二进制体积/`.text` 对不上 = 混杂信号。

## 便宜证据阶梯
```
① 已有采样文本 grep（零成本）→ ② 读源码控制流（零）→ ③ 算术量级推算（零）
→ ④ env 注入 + 单轮计时（低）→ ⑤ 重编译 + 完整对比（高）
```
前四步能排除的，绝不上第五步。
