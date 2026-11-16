# 引擎识别与专属能力路由

本文件负责把通用迁移流程落到具体向量引擎。识别顺序：源码树特征 → 构建产物/库名 → 符号/类名 → 用户声明。无法唯一判定时停止并请求确认，不得按“看起来像 Faiss”套用。

## 路由表

| 引擎 | 识别特征（任一命中） | Adapter | 专属能力（已安装时优先使用） |
| --- | --- | --- | --- |
| Faiss | `faiss/` 源码目录、`libfaiss`、`IndexFlat*`/`IndexIVF*`/`IndexHNSW`、`ProductQuantizer`、`DistanceComputer`、`InvertedListScanner` | `engines/faiss.md` | `faiss-source-analysis`、`faiss-benchmark-baseline`、`faiss-sra-test`、`faiss-perf-profile`、`faiss-simd-optimization`、算法专属 Optimization Skill |
| DiskANN | `diskann/`、`libdiskann`、`Vamana`、`GraphHeader`、`sector` 映射、`io_uring`、Rayon worker | `engines/diskann.md` | `diskann-source-analysis`、`diskann-benchmark-baseline`、`diskann-perf-profile`、`diskann-simd-optimization`、`diskann-io-optimization`、`diskann-memory-layout-optimization` |
| Milvus / 其他 | `milvus/`、`knowhere/`、其他引擎特征 | 无 | 通用 source-analysis / benchmark / 测试能力；必要时新建 adapter |

## 路由契约

1. **先加载 adapter 再定位源码**：adapter 给出该引擎的源码地图、基线口径、测试入口、库名与高风险位置。通用参考文件中的 Faiss/DiskANN 示例只作说明，不是当前引擎事实。
2. **专属 Skill 缺位时的降级**：对应引擎的 source-analysis / benchmark-baseline / 测试 Skill 未安装时，仍可执行通用流程，但必须把“缺少源码定位能力”“缺少冻结基线”“缺少测试入口”列为 blocker 或 `DEFER`，不得用记忆行号、历史收益或简化重实现补齐。
3. **不得跨引擎套用**：不得把 Faiss 的 `IndexIVF`/`ResultHandler` 风险表、`sra_test` 命令，或 DiskANN 的 sector/io_uring 结论写到另一引擎的迁移结论中。
4. **新引擎 adapter 最小内容**：引擎识别特征、源码位置规则、构建与基线入口、测试入口、ABI/序列化高风险点、性能采集口径、已知迁移案例。新增 adapter 放在 `references/engines/`，并在本表登记。

## 通用能力与专属能力的边界

| 能力 | 归属 |
| --- | --- |
| 资产分型、处置枚举、G0–G5、ABBA、追溯矩阵 | 本 Skill（通用） |
| 引擎源码调用链、实际内核选择、行号证据 | 对应引擎的 source-analysis Skill |
| 冻结性能/recall 基线、数据集与参数身份 | 对应引擎的 benchmark-baseline Skill |
| 测试框架构建、运行与输出解析 | 对应引擎的测试 Skill |
| CPU/IO/内存瓶颈采集与归因 | 对应引擎的 perf-profile 及专项 Optimization Skill |
| 引擎专属类名、序列化字段、风险位置 | 对应 adapter |
