# 阶段 1：环境与基线审计

任何新任务首先执行本阶段。目标是形成优化分支和干净基线都能复现的实验契约。

## 审计内容

- 阅读目标仓库 `AGENTS.md`、构建测试文档和当前工作树状态。
- 记录目标仓库类型：内部 KDNN 树或开源 VSAG fork。
- 固定 baseline commit、优化 commit、dirty state 和差异范围。
- 记录数据集、索引文件来源及校验信息，禁止跨分支混用不兼容索引。
- 记录 dimension、metric、top-k、`ef_search`、recall 和 batching flag。
- 记录 CPU/微架构、SVE/SVE2、内存、NUMA topology、物理 core set 和绑核方式。
- 明确区分物理核心数量、并发 `KnnSearch` 调用者数量、BLAS/OMP 线程数。
- 记录 batch size、timeout、warmup、重复次数和聚合方法。

## 基线要求

用与优化分支完全一致的参数运行干净 baseline。优先复用来源清楚且兼容的索引和构建产物；无法证明来源一致时重新生成。报告中不得使用历史峰值替代同配置基线。

## 阶段产出

输出一份基线审计记录，至少包含：commit、dirty state、数据集/索引、查询参数、硬件、NUMA/绑核、search/BLAS/OMP 线程、batch 参数、原始 QPS/latency/recall 和尚未满足的条件。信息不完整时状态为 `blocked`，不得进入性能归因。


