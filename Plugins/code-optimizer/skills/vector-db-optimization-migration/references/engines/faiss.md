# Faiss adapter

Faiss 是当前迁移能力覆盖最完整的引擎。本文件只保留 Faiss 专属事实与路由；资产分型、处置、G0–G5 与 ABBA 仍按通用参考执行。

## 1. 识别

任一命中即按 Faiss 处理：

- 源码目录 `faiss/`、`faiss/impl/`、`faiss/invlists/`；
- 构建产物 `libfaiss.so`、`libfaiss.a`；
- 类/符号：`IndexFlat*`、`IndexIVF*`、`IndexIVFFastScan`、`IndexHNSW`、`IndexScalarQuantizer`、`ProductQuantizer`、`DistanceComputer`、`InvertedListScanner`、`ResultHandler`、`VisitedTable`；
- 测试框架 `sra_test`。

## 2. 专属能力路由

| 阶段 | 路由 | 用途 |
| --- | --- | --- |
| 源码定位/调用链 | `faiss-source-analysis` | 在当前 commit 打开源码，确认宏作用点、模板/分发与实际内核；不沿用历史行号 |
| 冻结基线 | `faiss-benchmark-baseline` | 冻结 commit、构建 flags、数据集/GT、算法参数、线程/绑核、QPS/Recall 口径 |
| 测试构建与运行 | `faiss-sra-test` | 构建、运行 `sra_test`，解析 QPS/Recall；不得自行简化重实现 |
| 性能采集 | `faiss-perf-profile` | perf/计数/OpenMP/内存归因；迁移后热核必须重新采样 |
| 算法候选 | `faiss-*-optimization`（按算法） | RaBitQ / TurboQuant / IVFPQ / IVFPQFS / HNSW 的新候选设计，不属迁移 |

Faiss 专属 Skill 未安装时，按 `engine-routing.md` 的降级规则处理，不得凭记忆补行号或收益。

反向路由：当 Faiss 相关任务出现“跨版本迁移 / 版本升级 / 分支回主线 / 旧优化移植”意图时，应由对应 Faiss Skill 路由到本 Skill 的通用迁移流程，再由本 Skill 调用上表的 Faiss 专属能力取证；迁移门禁与裁决不在 Faiss 专属 Skill 内重复实现。

## 3. 高风险 ABI/序列化位置

| 位置 | 风险 | 迁移关注点 |
| --- | --- | --- |
| `Index*` 系列（Flat/IVF/IVFFastScan/Refine/HNSW） | 新增成员/虚函数导致布局与 vtable 变化 | 全局宏一致；copy/move/`reconstruct()`；序列化 |
| `DistanceComputer` | 新增虚函数或改变签名 | 所有派生类与调用点；链接期符号 |
| `ResultHandler` | 模板/虚函数变化影响 scan 与聚合 | 所有 handler 实例化；结果顺序契约 |
| `ProductQuantizer` | 码字布局、转置、LUT 构建变化 | 位级探针；序列化码字兼容 |
| `VisitedTable` | 布局/阈值/版本号变化 | 大表路径；tie-break；初始化语义 |
| `IndexIVF` scan / `InvertedListScanner` | 快路径与通用路径分支变化 | 两条路径都做 G2/G3 |
| `IndexIVFFastScan` | repack、bbs/qbs、TLS scratch | 每个 implementation 都验证 |
| `IndexHNSW` / `IndexFlatCodes` | quant_bits、codes 对齐、序列化 | 默认 32-bit 基准不被改变 |
| `index_read.cpp` / `index_write.cpp` | 版本常量、字段顺序、兼容分支 | 旧索引 load、save→load 往返 |

## 4. 迁移审计重点

- **构建注入**：厂商分支常通过 CMake option、per-file `-march`、`-flto` 注入收益；迁移前先区分“源码机制收益”和“非公平构建 flags 收益”，目标基线不得继承源分支的私有 flags。
- **厂商内核库**：独立 `*_krl/**` 或类似内核库应整体资产分型；目标版本没有对应库时，按 `REPLACE` 找上游等价实现或 `DROP`，不得把“链接厂商库”当迁移成果。
- **宏与公共头**：`#ifdef` 若包围类成员或虚函数，必须通过 public defines 全项目传播；否则跨 TU 类布局错位。
- **算法参数契约**：迁移不得修改 `nprobe`、`efSearch`、`nlist`、`k_factor`、`nbits`、`bbs/qbs` 或 recall 阈值来“换收益”；参数冻结由 `faiss-benchmark-baseline` 负责。
- **路径范围**：区分 `load` 索引与 `save` 构建索引；三组交叉验证（基线库@迁移索引 / 迁移库@迁移索引 / 基线库@基线索引）。

## 5. 门禁数据集覆盖

- 典型矩阵：`fashion`、`glove`、`sift`、`deep`、`gist`、`openai-1536-angular`、`openai-3072-angular`（以冻结基线实际矩阵为准）。
- 指标：以冻结的 recall@k 为准；迁移候选任一数据集低于基线即拒绝，含 1e-6 级微差。
- 维度/尾部：覆盖 128/256/768/960/1536/3072 等维度与 batch 16/8/4/2/1 尾部，具体以被测内核的维度门控为准。
- metric：L2 与 IP 分别覆盖；不得只测一种。

## 6. Faiss 工具命令

```bash
# 版本差异审计（CMake option / 宏 / 公共头）
python3 scripts/scan_version_delta.py --source <src_faiss> --target <dst_faiss> --json <run_dir>/version-delta.json

# 编译宏/flag 一致性（默认缺失/分裂即退出 1；--path-filter 限定到 Faiss 相关 TU）
python3 scripts/check_compile_flags.py --compile-commands <build>/compile_commands.json \
  --path-filter faiss/ \
  --require-macro <MACRO=VALUE> --require-flag=-march=<value> --json <run_dir>/compile-flags.json

# top-k 等价性对比（G0/G3；默认严格：查询集/条数/顺序/距离全等）
python3 scripts/compare_topk.py --baseline <base.dump> --candidate <cand.dump> --k 10
```

`scan_version_delta.py` 是启发式取证，发现项必须回到当前 commit 复核；`strings libfaiss.so` 不能证明宏已启用。`check_compile_flags.py` 按参数顺序解析 `-D`/`-U`（被 `-U` 取消的宏算缺失），`-march val` 与 `-march=val` 等价识别，某 TU 缺失 uniform flag 也算分裂。`compare_topk.py` 的 `overlap@k` 是与基线的重合率；G4 recall 用冻结 ground truth 每臂分别计算。
