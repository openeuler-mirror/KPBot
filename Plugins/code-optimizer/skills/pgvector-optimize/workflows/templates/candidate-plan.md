# 候选计划

## 实验与证据

run_id、源码/基线身份（扩展版本、Postgres 版本、构建宏）、目标（contract.algorithm）、工作负载、热点及采样口径、相关文件/符号（如 `HnswSearchLayer`、`VectorL2SquaredDistance`、`SelectNeighbors`、`GetScanPageItems`、`ElkanKmeans`）。

## 候选列表

每项包含：机制、guard、默认关闭宏/开关、fallback、改动范围、可移除成本上限、额外成本、数值/平台风险、UT/ST 与性能验证。

pgvector 常见机制与注意点：
- 限长距离（早停/裁剪）：仅适用于单调累加的距离（如 L2 squared）；须说明对 distance order/Recall 的预期影响。
- fmgr 开销削减 / 直通内核：注意函数地址/类型分派的版本差异，保留 fallback。
- SIMD/自动向量化：受 `OPTFLAGS`、`PG_CFLAGS`、`HALFVEC_DISPATCH`/`BIT_DISPATCH`/`VECTOR_TARGET_CLONES` 影响；平台差异要记录。
- 搜索状态/缓冲区预分配、k-means 不变量提升、build WAL 规避、FSM 页面复用、GUC 与并行度。

## 选择

排序依据、selected_candidate、selection_mode 及决策理由。相关实验记录列来源与适用条件；没有资料时说明当前依据。

## 可比性与执行边界

逐项确认冻结字段（GUC、索引、查询、数据）、质量要求（Recall@K、distance order）、资源约束、提交授权和停止条件；需要改变实验条件的方案另行声明。
