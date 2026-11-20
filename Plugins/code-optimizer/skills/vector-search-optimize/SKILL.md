---
name: vector-search-optimize
description: 编排向量数据库与向量检索的 CP0–CP7 优化流程。用于 ANN 索引、向量库、检索服务的性能诊断、基线、候选优化及复验。按实际目标路由到 Faiss、DiskANN、pgvector 专项工作流，其他引擎或服务层沿用通用流程。
---

# 向量检索优化

1. 确认优化层级、目标库或服务、源码版本、索引后端、基准入口、数据和资源范围。已有实验先读状态和产物，沿用其专项工作流和 `run_dir`。
2. 依据用户指定的优化对象、实际加载的后端和源码指纹选择工作流。

   | 实际优化对象 | 工作流 |
   | --- | --- |
   | Faiss 库、明确的 Faiss 索引或算子 | 加载 `faiss-optimize` |
   | DiskANN 库、明确的 DiskANN 磁盘索引或内存图 | 加载 `diskann-optimize` |
   | pgvector 扩展、PostgreSQL 向量索引（HNSW/IVFFlat）或距离内核 | 加载 `pgvector-optimize` |
   | VSAG 专项任务 | 加载现有 `vsag-rabitq-optimization` |
   | 其他引擎、向量数据库服务层或跨引擎对照 | 执行 [通用 CP0–CP7](references/cp0-cp7.md) |

3. `ANN`、`HNSW`、`PQ`、`RaBitQ` 等算法名仅用于缩小调查范围。以实际调用链定位被优化的层级。数据库依赖 Faiss 或 DiskANN 时，只有目标明确落在对应库的执行路径才交给专项工作流。无法确认目标时先做只读调查，再补齐影响路由的输入。
4. 把任务范围、后端证据、基准命令、数据与索引、`run_dir`、资源边界和交互模式传给专项工作流。专项接管后使用它自己的状态契约完成阶段，当前主 Agent 负责协调。通用入口与专项共用一套 CP 编号，每个实验只有一个阶段状态和产物目录。仅做源码调查、参数咨询或跑基线时执行对应范围。

通用流程复用 `vector-experiment-methodology` 的冻结条件与性能比较契约，以及 `vector-ut-st-validation` 的正确性与测试报告契约。自动模式沿用任务授权，提交、推送和发布按用户明确给出的范围处理。
