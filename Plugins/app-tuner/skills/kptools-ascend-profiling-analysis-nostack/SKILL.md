---
name: kptools-ascend-profiling-analysis-nostack
description: |
  分析 torch_npu/Ascend profiler 的无栈 trace（trace_view.json 中无 python_function）。
  能力：在 trace 没有 ProfilerStep#N 的情况下识别 step 边界与每步时间；用设备内核 +
  host 算子 + vllm/vllm-ascend 源码知识近似重建 P1/P2/P3、模型结构和 Free 去向。
  当用户提供无栈 trace、询问 step 切分、Free 时间去哪了、host 在干什么，
  或 trace 的 with_stack=false、没有 python_function 事件时使用。
  有栈 trace 请使用 kptools-ascend-profiling-analysis。
---

# Ascend 无栈 Profiling Step 分析

## 适用范围

- trace 中 **没有 `python_function`**（`with_stack=false`），因此无法用 Python 帧切 step
- trace 中 **没有 `ProfilerStep#N`**，或即使有也不作为判据（本 skill 一律忽略该标识）
- 有栈 trace 请使用 `kptools-ascend-profiling-analysis`

## 输入

必需：`trace_view.json`

可选侧车（与 trace 同目录，按精度优先级自动查找）：

| 优先级 | 文件 | 提供 | 精度 |
|---|---|---|---|
| 1 | `ascend_pytorch_profiler_*.db` 的 `STEP_TIME` | 每步 startNs/endNs | **精确**（与 ProfilerStep 逐 ns 一致） |
| 2 | `kernel_details.csv` 的 `Step Id` + `Start Time(us)` | step 编号 + 设备侧起止 | 设备侧近似（起点晚于 host step start） |
| 3 | `step_trace_time.csv` 或 `analysis.db:StepTraceTime` | 步数 + 每步时长 | 无绝对起点，需用 trace sync 对齐 |
| 4 | `communication.json` 或 `analysis.db:CommAnalyzerTime` | `step0..stepN` 步数 | 无绝对起点，需用 trace sync 对齐 |
| 5 | 仅 `trace_view.json` | 设备级 sync 结束 + host 首个事件 | 最差情况近似 |

`_0.db` / `_1.db` 都存在时扫描全部，取含 `STEP_TIME` 行数最多的一个。

## 工作流

1. 判断是否无栈：trace 无 `python_function` 事件，或 `profiler_info_*.json` 中 `with_stack=false`
2. 运行：

```bash
python3 scripts/detect_steps.py <trace_view.json>
```

输出：

```
source: step_time (exact, host)
steps: 4
  #0  UI     53.221 ~     77.331 ms  dur    24.110 ms
  ...
```

机器可读输出：

```bash
python3 scripts/detect_steps.py <trace_view.json> --json
```

强制走某一级（调试/降级验证）：

```bash
--source step_time|kernel_details|step_trace_time|comm|trace
```

3. 报告 step 时同时给出 `source`、`confidence`、`scope`，不要只给数字

## HTML 报告

```bash
python3 scripts/report_html_nostack.py <trace_view.json> --out report.html
```

报告结构与有栈 HTML 保持同一套视觉/导航：

- 首页与每个 step 页顶部：Computing vs Free 重点框（只比较这两类，Communication 不在此框内）、整窗四分类、每步汇总、阶段与 Free 去向、模型结构、最高 Free 步的近似调用栈、结论、Host API/算子 Top
- 每步一页：四分类总览、P1/P2/P3 阶段切分、模型结构、近似调用栈、Free 去向、计算算子 Top、通信明细
- 无 Python 栈，因此不生成火焰图；P1/P2/P3、模型结构和调用栈均为近似
- 报告默认按 vllm 0.23.0 / vllm-ascend 0.23.0rc1 的源码结构命名；其他版本需核对源码

### 近似方法

- **P1/P2/P3**：由设备内核顺序推导。P1 = step 起点到首个 forward 内核；P2 = 首个 forward 内核到最后一个 logits MatMul；P3 = 其余到 step 结束
- **模型结构**：取主执行流内核序列，找重复 attention 锚点，按 QKV/O/MLP-up/Act/MLP-down/Norm/KV写 标注
- **Free 去向**：每个 Free 区间归到最近设备内核（或该阶段内最近的算子），再映射到源码函数；同时检查 host 是否持续有事件
- **限制**：不能还原真实 Python 调用栈；Free 归因是 operator/phase 级近似，通信只作了解

## 最差情况：只有 trace_view.json

1. 取 host 进程（含 `cpu_op`/`enqueue`/`dequeue` 最多的 pid）的第一个事件作为 step0 start
2. 取 `aclrtSynchronizeDevice*` 的结束时刻作为后续 step 边界
3. 过滤退出 sync：两次 sync 之间没有 Overlap Analysis 的 Computing/Communication 活动时，丢弃后一个
4. 没有 device sync 时依次回退 `SynchronizeStream` / `SynchronizeEvent`
5. 一个 step 内出现多个同类 sync 时，用 `--min-step-ms` 合并间隔过近的边界（保留靠后）
6. 尾部退出 sync 造成的极短 step 默认丢弃（`--trailing-min-ms 0.5`）

已知限制：

- step0 start 只能取到"首个可观测 host 事件"；Mixtral eager 实测可能晚 5ms
- 设备 sync 边界比 ProfilerStep end 早约 0.02~0.10ms（ProfilerStep 是 host 侧标注）
- step0 之前的 warmup 不会被算进 step0

## 输出约定

- 时间统一为 `UI ms`：`ui_zero` = trace 全部事件最早 ts（与查看器零点一致）
- `scope`：`host`（step_time / trace sync）或 `device`（kernel_details）
- `confidence`：`exact` / `approx` / `low`
- 侧车步数与 trace 边界数量不一致时，在 `notes` 中说明并降低置信度

## 当前版本范围

- v1：step 识别 + 每步时间 + HTML 报告（P1/P2/P3、模型结构、近似调用栈、Free 去向）
- 后续继续完善：更细的 P1 子阶段、与 with-stack 真值的误差评估、版本自适应映射

## 开发校验

```bash
# 仅在带 ProfilerStep 的数据上验证误差；ProfilerStep 不参与识别
python3 scripts/detect_steps.py <trace_view.json> --validate
```
