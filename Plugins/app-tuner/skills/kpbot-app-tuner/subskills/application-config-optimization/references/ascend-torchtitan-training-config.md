# Ascend NPU + torchtitan 训练应用配置实战经验

本文件沉淀 Ascend910 + torchtitan-npu (DeepSeek-V4 debug) 训练优化案例中应用配置层的正向实测经验。所有收益数据仅作参照，实际收益随模型/卡型/负载而异。

> **调用约定**：本 reference 由 `SKILL.md` 按需引用，不作为主流程的独立阶段。

## 1. 案例环境

| 项目 | 值 |
|------|-----|
| 应用 | torchtitan-npu (DeepSeek-V4 debug, 307M params, 43 layers, 4 experts) |
| 硬件 | 2 × Ascend910 (Ascend910_9362, 64GB HBM) |
| 容器 | ubuntu-npu (Docker --privileged --network host) |
| CPU | 40 cores aarch64, single NUMA, no SMT |
| 软件 | PyTorch 2.12.0+cpu, torch_npu 2.12.0.rc1, CANN 9.1.0 |
| 训练配置 | seq_len=256, 10 steps, 2 NPU, expert_parallel_degree=2, profiling step 5 |
| 基线 HBM | 2.18 GiB (3.56%) — 充足，可换内存换速度 |

## 2. 正向实测收益

三轮平均，单变量原则，每轮独立清理 kernel cache 后验证。

| 参数 | 基线 Free time | 优化后 Free time | 收益 | 基线 Node@launch 间隔 | 优化后 Node@launch 间隔 | 收益 | 备注 |
|---|---|---|---|---|---|---|---|
| `--optimizer.no_swap_optimizer` | 2,539,756 us | 1,988,708 us | **-21.7%** | 74.82 us | 60.89 us | **-18.6%** | 消除 16 次 H2D/D2H swap 同步；HBM +2.3GB |
| `--activation_checkpoint.mode none` | 1,988,708 us | 1,380,149 us | **-30.6%** | 60.89 us | 52.65 us | **-13.5%** | 消除 backward 重计算；HBM 2.18→6.97 GiB |
| `--parallelism.fsdp_reshard_after_forward never` | 1,380,149 us | 1,258,584 us | **-8.8%** | 52.65 us | 52.41 us | **-0.5%** | 消除 backward re-gather；Node@launch 数 40223→30923 |
| **3 项累计** | **2,539,756 us** | **1,258,584 us** | **-50.4%** | **74.82 us** | **52.41 us** | **-30.0%** | 稳态步 2.10s→1.17s |

## 3. 参数详解

### `--optimizer.no_swap_optimizer`

| 字段 | 值 |
|------|-----|
| 配置位置 | CLI 参数 (EXTRA_ARGS)，tyro 风格 `--optimizer.no_swap_optimizer` |
| 作用 | 禁用 SwapOptimizer，optimizer state (exp_avg + exp_avg_sq) 全部保留在 NPU HBM，消除每 step 16 次 H2D copy → wait_event → update → D2H copy 的流水线同步 |
| 代码路径 | `optimizer_selector.py` 检测 `swap_optimizer=False` → `_build_standard_optimizer` → 创建普通 `OptimizersContainer`，不 patch `AdamW.step` |
| 适用场景 | HBM 使用率 <30%（optimizer state 占总 HBM 比例小） |
| HBM 增量 | ~2.3GB（307M params × 2 states × fp32 = 2.3GB） |
| 风险 | 低：HBM 充足时无内存压力 |
| 验证 | 训练日志应显示 `Using standard Optimizer`（非 `Using SwapOptimizer`）；profiling `Optimizer.step` host total 从 ~707ms 降至 <50ms |
| 回退 | 移除该 flag |

> **决策规则**：HBM <30% → `no_swap_optimizer`；30-60% → `swap_optimizer_times=1`（单批 swap）；>60% → 保持默认 16。

### `--activation_checkpoint.mode none`

| 字段 | 值 |
|------|-----|
| 配置位置 | CLI 参数 (EXTRA_ARGS) |
| 作用 | 禁用激活检查点，所有 forward activation 保留在 HBM，backward 直接用缓存的 activation 计算梯度，不需重跑 forward |
| 代码路径 | `trainer_base_config` 默认 `mode="full"`；CLI 覆盖为 `none` 后 `ActivationCheckpointConfig` 跳过 checkpoint 包装 |
| 适用场景 | HBM 使用率 <40%（所有层 activation 可全部驻留 HBM） |
| HBM 增量 | ~860MB（43 层 × ~20MB/层，seq_len=256 bf16） |
| 风险 | 低：HBM 充足时无内存压力 |
| 验证 | profiling backward 阶段不应出现 forward recomputation kernel（MHCPostTritonBackward 数量减半） |
| 回退 | 移除该 flag（恢复 full） |

> **决策规则**：`HBM_usage + AC_activation_size < 80% HBM_total` → `none`；否则保持 `full` 或用 `selective`。

### `--parallelism.fsdp_reshard_after_forward never`

| 字段 | 值 |
|------|-----|
| 配置位置 | CLI 参数 (EXTRA_ARGS) |
| 作用 | forward 后不释放 FSDP 分片参数（不 reshard），backward 不需 re-all_gather 重新收集参数 |
| 代码路径 | `trainer_base_config` 默认 `"always"`；CLI 覆盖为 `never` 后 `apply_fsdp` 的 `reshard_after_forward_policy="never"` |
| 适用场景 | HBM 使用率 <50%（全参数驻留 HBM） |
| HBM 增量 | ~300MB（半个模型参数大小，bf16 sharded → full） |
| 风险 | 低 |
| 验证 | profiling backward 阶段 `hcom_allGather` 次数减少 ~50%；Node@launch count 减少 ~23% |
| 回退 | 移除该 flag（恢复 always） |

> **决策规则**：`HBM_usage + unshard_param_size < 70% HBM_total` → `never`；否则保持 `always`。

## 4. 参数冲突矩阵

| 参数 A | 参数 B | 能否共用 | 说明 |
|--------|--------|----------|------|
| `--optimizer.no_swap_optimizer` | `--optimizer.swap_optimizer_times N` | ❌ 互斥 | no_swap_optimizer 直接禁用 swap，不需要 times |
| `--activation_checkpoint.mode none` | `--compile.enable` | ✅ 可共用 | AC=none 反而消除了 compile+AC 的已知 warning |
| `--parallelism.fsdp_reshard_after_forward never` | `--optimizer.no_swap_optimizer` | ✅ 可共用 | 两者都增加 HBM 用量但互补：swap 省的是 optimizer state，reshard 省的是 param 分片 |

## 5. HBM 容量决策树

```
当前 HBM 使用率（含模型权重 + optimizer state + activation）:
  │
  ├─ <30%: 可同时启用全部 3 项
  │   ├─ --optimizer.no_swap_optimizer        (+~2.3GB)
  │   ├─ --activation_checkpoint.mode none     (+~860MB)
  │   └─ --parallelism.fsdp_reshard_after_forward never  (+~300MB)
  │
  ├─ 30-50%: 部分启用
  │   ├─ --optimizer.swap_optimizer_times 1   (减少 swap 批次，不完全禁用)
  │   ├─ --activation_checkpoint.mode none     (如 HBM 增量后 <60%)
  │   └─ --parallelism.fsdp_reshard_after_forward never  (如 HBM 增量后 <60%)
  │
  ├─ 50-70%: 谨慎选择
  │   ├─ --optimizer.swap_optimizer_times 2   (减少 swap 批次)
  │   └─ --parallelism.fsdp_reshard_after_forward never  (增量最小)
  │
  └─ >70%: 保持默认，通过其他 skill 优化
```

## 6. 验证方法论

每个参数变更执行至少 3 轮独立训练，取 profiling step 5 的三轮平均值：

1. 每轮独立清理 kernel cache 和 profiling 输出
2. 提取指标：`python3 scripts/extract_baseline.py <profiling_dir>`
3. 计算三轮平均，对比基线：`gain_pct = (avg - baseline) / baseline * 100`
4. `gain < -1%` → 保留；`gain > +1%` → 回退；中间 → 增加轮次

## 7. 关键指标

从 CANN profiling 的 `step_trace_time.csv` 和 `trace_view.json` 提取：

| 指标 | 来源 | 基线值 | 3 项优化后 | 收益 |
|------|------|--------|-----------|------|
| Device Free time | `step_trace_time.csv → Free` | 2,539,756 us (84.4%) | 1,258,584 us (~42%) | -50.4% |
| Node@launch 平均间隔 | `trace_view.json → Node@launch ts diff mean` | 74.82 us | 52.41 us | -30.0% |
| Node@launch count | `trace_view.json → count(Node@launch)` | 40,223 | 30,923 | -23.2% |
| Optimizer.step host total | `operator_details.csv` | 707,549 us | <50,000 us | -93% |
