# DiskANN adapter

本文件只保留 DiskANN 专属事实与路由；资产分型、处置、G0–G5 与 ABBA 仍按通用参考执行。

## 1. 识别

任一命中即按 DiskANN 处理：

- 源码目录 `diskann/`、`diskann_rs/`、`src/` 下的 Vamana/索引实现；
- 构建产物 `libdiskann*` 或 Rust 可执行 benchmark；
- 符号/结构：`Vamana`、`GraphHeader`、`sector`、`PQ`/`LUT`、`scratch`、`cache`、`io_uring`、Rayon worker；
- 资源配置以 `.toml`/CLI 参数冻结。

## 2. 专属能力路由

| 阶段 | 路由 | 用途 |
| --- | --- | --- |
| 环境与索引身份 | `diskann-env-check` | 源码版本、CPU ISA、NUMA、编译器/Rust、内核与 io_uring、存储/文件系统、索引元数据 |
| 源码定位/调用链 | `diskann-source-analysis` | 在当前 commit 追踪构建/查询路径、sector 映射、PQ/LUT、scratch、cache、Rayon 与 io_uring |
| 冻结基线 | `diskann-benchmark-baseline` | 冻结数据集/索引身份、构建契约、查询参数、CPU/NUMA 绑核、cache/预热、Recall/IOs/Comps/Hops、重复次数与原始产物 |
| 性能采集 | `diskann-perf-profile`、`diskann-bottleneck-diagnosis` | CPU/调度/内存/块 I/O 联合证据；按源码阶段、计数、profile、off-CPU、cache、SSD I/O 归因 |
| 专项优化 | `diskann-simd-optimization`、`diskann-io-optimization`、`diskann-memory-layout-optimization` | 仅在 profile 证明目标热点占比后使用 |
| 验收 | `diskann-experiment-review` | 改动范围、feature gate、Recall/IOs/Comps/Hops、ABBA 漂移、产物完整性 |

## 3. 高风险位置

| 位置 | 风险 | 迁移关注点 |
| --- | --- | --- |
| `GraphHeader` / 索引元数据 | 版本号、字段顺序、sector 计数变化 | 旧索引 load 与 save→load 往返；不得只验证新构建索引 |
| 节点记录 / sector 装箱 | 记录大小、对齐、跨 sector 变化 | 读放大与 `io_uring` 读取次数；G3 搜索输出与 IOs 对照 |
| PQ pivots/codes/LUT | 布局、量化参数、LUT 构建变化 | 位级/距离一致性；4-bit/8-bit 分别覆盖 |
| `visited` 结构（HashSet/bitmap） | 布局、版本号、阈值变化 | tie-break、初始化语义、大表路径 |
| 候选缓冲区 / scratch | 复用语义、容量上限变化 | 并发与生命周期；UT/ST 覆盖复用路径 |
| cache / 预热策略 | cache 大小或命中语义变化 | 不得用 cache 改动冒充实现收益；基线 cache 策略冻结 |
| 查询参数（L/beam 等） | 迁移时顺手调参 | 比较纯实现优化时不得调整 L、beam、cache 大小或召回阈值 |
| 存储 I/O 路径 | `O_DIRECT` 对齐、SQE/CQE 批处理、注册缓冲区 | 读放大与提交/等待语义；需物理布局对照才可宣称布局收益 |

## 4. 迁移审计重点

- **构建身份**：记录 Rust/编译器版本、feature flags、目标 ISA 与 profile；feature-off 必须回到目标上游默认构建。
- **索引身份**：迁移前后的索引必须可区分来源；旧索引 load 与新构建索引分别验证，不能混用。
- **正确性口径**：Recall 之外同时报告 IOs、Comps、Hops；任一数据集 recall@10 低于冻结基线即拒绝（零下降门禁）。
- **参数冻结**：迁移不得调整 L、beam、cache 大小、召回阈值或数据集来换收益；这些属于基线参数漂移。
- **I/O 归因**：只有在 profiling 证明 I/O 或 I/O/CPU 重叠占比较高时才进入 I/O 优化；不得仅凭数据集 N/D 推断因果。

## 5. 门禁数据集覆盖

- 以 `diskann-benchmark-baseline` 冻结的数据集/索引矩阵为准；至少覆盖大 N、高维与低维、不同 metric。
- 每个数据集记录 Recall、IOs、Comps、Hops、QPS 与原始产物路径；ABBA 交错并标定噪声地板。
- cache/预热策略与基线一致；比较实现优化时不得改变 cache 大小。

## 6. 工具命令

通用脚本中 `scan_version_delta.py` 只适用于类 CMake 构建；DiskANN 使用 Rust/Cargo 或自建构建时，构建一致性改用 `compile_commands.json` 或 Cargo 构建日志取证，并在 adapter 允许后使用 `check_compile_flags.py`。top-k 对比仍可使用 `compare_topk.py`，但输入 dump 必须来自真实查询路径并同时保留 IOs/Comps/Hops。
