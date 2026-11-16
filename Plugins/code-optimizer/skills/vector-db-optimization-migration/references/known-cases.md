# 迁移案例库

本文件只保留“机制 → 类型 → 处置 → 门禁重点”的可复用结构，用于加速新版本对的迁移分型。案例 A–C 为 Faiss 历史案例，仅作分型与门禁思路参考；其他引擎按同样结构登记，不得直接照抄其类名、宏或处置。

## 案例模板（其他引擎）

| 字段 | 填写要求 |
| --- | --- |
| engine / 版本对 | 引擎与源/目标版本；对应 adapter |
| 源资产 | 机制、类型、源位置、历史动机 |
| 目标差异 | 作用点是否存在、API/ABI/布局/参数是否变化 |
| 处置 | `KEEP/ADAPT/SPLIT/OPT_IN/REPLACE/DROP/DEFER` 与理由 |
| 门禁重点 | G0–G5 中需要额外覆盖的路径与边界 |
| 复用限制 | 不得沿用的历史数字、版本假设与个人路径 |


> 去敏与复用规则：
> - 不迁移具体版本 commit、QPS/加速比、实验日期、索引/数据集 SHA 与个人路径。
> - 案例中的历史收益只能说明“该机制曾有价值”，不能作为目标版本预期；一切收益按 `equivalence-and-gates.md` 重新测量。
> - 复用前必须用对应引擎的 source-analysis Skill 在当前源/目标 commit 重新定位作用点，行号不得沿用；Faiss 案例使用 `faiss-source-analysis`。

## 案例 A（Faiss）：厂商增强版 → 上游通用版

### 背景

- 源：带厂商内核库（独立 `*_krl/**` 类内核库）与构建覆写（`-march` / `-flto` / `-funroll-loops`）的 Faiss 分支。
- 目标：上游 Faiss + 仅 AArch64 NEON/SVE intrinsic，不链接厂商库，不使用非公平构建 flags。
- 关键风险：历史收益很大比例来自厂商内核库与构建 flags，不能直接当作通用版预期。

### 资产分型与处置

| 资产 | 类型 | 处置 | 迁移要点 |
| --- | --- | --- | --- |
| 厂商内核库目录（`*_krl/**`） | `kernel_library` | `DROP` | 整体不迁移；目标版无对应库，逐符号找上游等价实现 |
| CMake 厂商开关 / per-file `-march` / `-flto` 覆写 | `build_only` | `DROP` | 统一 Release + `FAISS_OPT_LEVEL`，无 LTO；避免非公平基线 |
| 厂商 PQ 转置 LUT、两级 reorder、FastScan bs32 repack | `kernel_library` / `macro_optimization` | `REPLACE` | 用上游 `compute_distance_table(s)`、`reorder_2_heaps`、上游 bs32 kernel |
| 连续码快路径（CONTIGUOUS + WIDE + BLOCK_HEAP_GATE） | `macro_optimization` | `KEEP` | 纯 Faiss 侧；WIDE 与 CONTIGUOUS 必须绑定，单独开可能无作用点 |
| L2 early-abandon（STEP + NEON + SVE batch） | `macro_optimization` | `KEEP` | 原调用点可能被厂商宏包住，迁移时需移出并保留 kill-switch |
| IVFPQ scanner SVE dispatch、IP 粗距离 `dis0` 复用 | `structural_enabler` | `KEEP` | 是 SVE 快路径前置条件，先于算法宏迁移 |
| HNSW 版本化 bit visited table（1 bit/元素 + per-word 版本号） | `structural_enabler` | `KEEP` | 大表优先；tie-break 与阈值需在目标版本复测 |
| HNSW MinimaxHeap SVE、邻接预取、IndexFlat 预取、batch IP | `macro_optimization` | `KEEP`（仅 Faiss 侧） | 厂商库内部的同名 V5 部分不迁移 |
| HNSW BFS 图重排；SQ8/SQ16 存储与距离 | `structural_enabler` | `OPT_IN` / `DEFER` | 改变结果顺序或量化契约，默认关闭 |
| IVFFastScan TLS scratch；nprobe clamp / `skip_coarse` | `non_macro_source` | `KEEP` | 消 malloc/free、边界语义；非宏项也要建 patch |
| CQ reorder skip（结果顺序不敏感场景） | `macro_optimization` | `OPT_IN` | 仅对顺序不敏感数据集/场景显式开启 |
| 预计算表预取、reservoir cap、`ny` batch16 | `macro_optimization` | `DROP` | 中性/负向或作用点缺失；不要为了“整集”保留 |
| IVF 大 k 跳过最终 reorder | `macro_optimization` | `OPT_IN` | 输出由有序变堆序，属结果契约变化 |
| `IxQq` / 量化 flat 序列化 | `structural_enabler` | `ADAPT` | 与目标序列化版本对齐；能力项默认关 |

### 建议 patch 序列（去厂商库后）

1. `memory`：通用对齐分配器 + prefetch 工具（改名去掉厂商命名）。
2. `hnsw-vtbits`：VisitedTableBits + 阈值路由 + HNSW 分发。
3. `v5-faiss`：MinimaxHeap SVE、邻接/IndexFlat 预取、batch4 IP、PQ 8-code SVE（仅 Faiss 侧）。
4. `l2ab`：early-abandon STEP/NEON/SVE（从厂商宏中移出）。
5. `ivfpq-scan`：CONTIGUOUS + WIDE + gate + SVE dispatch + IP_DIS0。
6. `ivfpqfs-fastscan`：TLS scratch + nprobe clamp/skip_coarse + CQ_REORD（OPT_IN）。
7. `optional`：SKIP_LARGE_K、FORCE_INLINE_DISTANCE8（OPT_IN/宏关）。

### 门禁重点

- feature-off 与上游逐位一致；扫描厂商宏/禁词，确认没有残留依赖。
- `compile_commands.json` 检查每个相关 TU 的 `-D`、`-march`、`-flto` 一致。
- HNSW 大批量距离、IVFPQ 连续码快路径、IVFPQFS FastScan 分别做 G2 位级探针。
- 旧版本 accepted 的宏必须在目标版本重新做单变量门禁；组合收益另开实验。

### 常见坑

- 宏默认 OFF 导致“已移植但未生效”。
- 只迁移 Faiss 侧，遗漏厂商库内部同名的 V5/vtbits 路径。
- 去掉厂商库后对象布局与宏集合变化，旧“整集布局承重”结论失效。
- 码字对齐、分块 L2、维度路由在高维数据集上收益结构变化，需按维度门控重新判定。

## 案例 B（Faiss）：HNSW 增量优化跨版本补齐

源分支只迁移了部分 HNSW 机制时，目标版本常出现“看起来迁了但没有效果”。按以下缺口逐项核对：

| 缺口 | 类型 | 处置 | 验证 |
| --- | --- | --- | --- |
| 批量距离主路径缺失，热路径仍逐条算距离 | `structural_enabler` | `ADAPT` | 反汇编/采样确认热路径确实调用批量接口 |
| 只迁了 Faiss 侧，内核库内的批量内核未迁 | `kernel_library` | `REPLACE` | G2 位级探针对比批量输出 |
| codes 对齐收益在通用版缩水 | `structural_enabler` | `DEFER` | 目标版本布局复测后再决定 |
| `d>128` 分块 L2 内核缺失 | `macro_optimization` | `ADAPT` | 覆盖 128/256/768/960/1536/3072 维度 |
| 维度路由/量化修复只存在于厂商库 | `kernel_library` | `REPLACE` / `DEFER` | 按 metric 分支做位级探针 |
| 编译 flags 差异（LTO/`-funroll-loops`） | `build_only` | `DROP`/`KEEP` | `compile_commands.json` 一致性检查 |

门禁补充：批量距离路径除 top-k 一致外，还要验证尾部不足 batch、维度门控两侧、L2/IP 两种 metric。

## 案例 C（Faiss）：IVFPQ / IVFPQFS 通用化

| 机制 | 类型 | 处置 | 迁移要点 |
| --- | --- | --- | --- |
| 宏集合与厂商禁词残留 | `build_only` | `DROP` | 先做宏清单与禁词扫描，再谈收益 |
| L2 early-abandon 调用点 | `macro_optimization` | `KEEP` | 从厂商宏移出；保留 feature-off |
| L2AB 高维门控（仅特定维度/算法生效） | `macro_optimization` | `OPT_IN` | 门控条件写清楚，避免全局回归 |
| LUT 构建优化 | `macro_optimization` | `DEFER` | 必须先用 `faiss-perf-profile` 证明 LUT 构建占比 |
| FastScan SVE 内核 / TBL4/TBL8 查表 | `kernel_library` / `macro_optimization` | `REPLACE` / `ADAPT` | 用上游 kernel 等价重写，按实现分支验证 |
| TLS scratch 覆盖所有 FastScan 实现 | `non_macro_source` | `KEEP` | 确认每个 implementation 都走 scratch |
| `nprobe` clamp / `skip_coarse` 语义 | `non_macro_source` | `KEEP` | 边界：nprobe ≥ nlist、全表扫描 |
| CQ_REORD / SKIP_LARGE_K | `macro_optimization` | `OPT_IN` | 结果顺序契约变化，默认关 |
| REPACK / 码字布局 | `structural_enabler` | `DEFER` | 需要强证据；无证据不要改布局 |

门禁补充：IVFPQ/IVFPQFS 要分别覆盖 `scan_codes` 快路径命中/未命中、HighDim 分块、`k` 大于堆阈值与 `nprobe` 边界。

## 如何把案例映射到新版本对

1. 先按 `migration-contract.md` 做资产分型，不要直接照抄案例处置。
2. 对每个 `KEEP`/`ADAPT` 项在目标版本重新定位 `path::symbol`，确认作用点存在。
3. 对每个 `REPLACE` 项写清上游等价实现与差异（尤其归约顺序、tie-break、量化语义）。
4. 对 `OPT_IN`/`DEFER` 项写清触发条件与重新评估门槛。
5. 所有收益与 recall 结论按 `equivalence-and-gates.md` 重新采集，不引用历史数字。
