# kptools-ascend-profiling 使用指南

## 1. 两方案选择

| | 方案一 Ascend PyTorch Profiler | 方案二 MS Service Profiler |
|---|---|---|
| 粒度 | PyTorch 算子级 | 服务框架函数级 + 算子级 |
| 控制 | API（/start_profile → /stop_profile） | JSON 配置 enable: 0/1 |
| 前置 | vLLM 以 --profiler-config 启动 | SERVICE_PROF_CONFIG_PATH 启动 |
| 输出 | ascend_pt 目录（7 类文件） | Chrome Tracing + CSV（5 类文件） |
| 适用 | 算子性能深度分析 | 服务框架流程分析（Request/KVCache/Batch） |

## 2. 方案一产物解读

| 文件 | 内容 | 常用度 |
|------|------|--------|
| operator_details.csv | 每个算子执行详情 | ★★★ 算子热点排序 |
| op_statistic.csv | 算子利用率统计（按 op 聚合） | ★★★ |
| step_trace_time.csv | 迭代分解（feed/forward/backward/换卡） | ★★★ 训练场景 |
| kernel_details.csv | kernel 级数据 | ★★ |
| trace_view.json | Chrome Tracing（MindStudio Insight 可视化） | ★★★ |
| api_statistic.csv | API 调用统计 | ★ |
| analysis.db | 性能数据库 | ★ |

## 3. 方案二 domain 与产物

| Domain | 内容 | 产物 |
|--------|------|------|
| Request | 请求生命周期 | request.csv |
| KVCache | KV Cache 管理 | kvcache.csv |
| BatchSchedule | Batch 调度 | batch.csv |
| Communication | 通信（HCCL） | chrome_tracing.json |
| ModelExecute | 模型执行 | chrome_tracing.json |

## 4. A+K 异构分析（与 kptools-py-spy 配合）

1. CPU/Python 侧：kptools-py-spy record（data loader/预处理/Python 开销）
2. NPU 侧：本 SKILL（算子执行、HCCL）
3. 对齐时间线：CPU 侧"等待"区间 vs NPU 侧"空闲"区间 → 判定 feed 债务/算子瓶颈/通信瓶颈

## 5. 约束

- profiling 期间 npu-smi AICore(%) 返回 0（ascend-smi 指南已注明），勿误判为故障
- 输出目录为服务端 torch_profiler_dir / ~/.ms_server_profiler；容器场景注意卷挂载
- MindStudio Insight 打开 trace_view.json / chrome_tracing.json 做可视化
