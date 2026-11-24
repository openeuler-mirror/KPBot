---
name: kptools-ascend-profiling-step-split
description: |
  对没有可用 step 标识的 Ascend/torch_npu trace 做深度 step 切分：多信号投票
  （设备 sync、设备忙闲周期、host 下发批次、idle gap 分层），输出每个边界的时间、
  置信度与依据；有 STEP_TIME / ProfilerStep / kernel_details Step Id 时自动做权威自检
  并给出 F1 标定。适用于“trace 里没有 ProfilerStep#N、侧车缺失或残缺、现有切分不准、
  需要按 decode 迭代粒度重新切 step”的场景。
  通用有栈/无栈 profiling 分析仍走 kptools-ascend-profiling-analysis /
  -analysis-nostack；两份 trace 的对比走 kptools-ascend-profiling-compare-hub（已合入 master）；
  注：prefill-decode 与本 skill 为同批 PR 交付，合入前阶段判别暂用 -analysis-nostack 承接。
  本 skill 只回答“step 边界在哪里、有多可信”。
---

# 无标识 trace 深度 Step 切分

## 适用

- `trace_view.json` 中**没有可用的** `ProfilerStep#N`，或没有 `STEP_TIME` / `kernel_details.csv` 等权威侧车。
- 有权威标识，但想验证现有切分是否可信（`--authority` + 自检）。
- 需要按 decode 迭代粒度（而不是 profiler 的粗窗口）重新切 step。

不适用：

- 通用单份 trace 分析（P1/P2/P3、模型结构、Free 去向）→ `kptools-ascend-profiling-analysis-nostack` / `kptools-ascend-profiling-analysis`。
- 两份 trace 的整体/专项对比 → `kptools-ascend-profiling-compare-hub` 及对应专项 skill。
- 只想知道每步四分类数值 → 直接读 `analysis.db:StepTraceTime`。

## 输入

| 输入 | 必需 | 说明 |
|---|---|---|
| `trace_view.json` | 是 | 全量事件；本 skill 只依赖它也能工作 |
| `ascend_pytorch_profiler_*.db` | 否 | `STEP_TIME` 作为权威边界与自检基准 |
| `kernel_details.csv` | 否 | `Step Id` + `Start Time(us)` 作为设备侧权威边界 |
| `ProfilerStep#N` 事件 | 否 | trace 内权威边界（有则用于自检，不参与识别） |

路径可以是 capture 目录、`ASCEND_PROFILER_OUTPUT` 目录或裸 `trace_view.json`；侧车从 trace 所在目录及其父目录自动查找。

## 工作流

```bash
python3 scripts/split_steps.py <trace_view.json> \
  --json result.json \
  --out-html report.html \
  [--min-step-ms 0.5] [--tolerance-ms 2] [--authority] [--no-self-check]
```

1. 默认按**迭代粒度**切分（空档阈值 0.5ms = `--min-step-ms`，硬下限 0.3ms）：适合 decode 循环，一个迭代一个 step。
2. 需要与 profiler 的粗 step 对齐时调大阈值，例如 `--min-step-ms 5~20`。
3. 有权威侧车时会自动输出 `self_check`（precision/recall/F1/最大误差）；`--authority` 则直接采用权威边界。
4. 先看 `data_quality.confidence` 与 `self_check.f1`，再看 `per_step[*].uncertain`。

## 信号与融合

| 信号 | 作用 | 权重 |
|---|---|---|
| `idle` | 设备忙闲空档 ≥ 阈值时，取空档起点为边界 | 0.55 |
| `sync` | 与候选空档起点对齐的 `Synchronize*` 结束作为佐证；无 idle/host 候选时按稳定 sync 周期兜底 | 0.30 |
| `period` | 空档间隔的重复性（主导周期）对齐的边界 | 0.25 |
| `host` | host 空闲后恢复下发（且伴随 enqueue）的批次，边界取空档起点（与 idle 同参考点；ProfilerStep# 包装事件不参与 host 统计） | 0.15 |

- 边界置信度 = 出现信号权重和 × 信号自身分数，上限 1.0；`< --min-confidence` 或仅单一信号时标 `uncertain`。
- 设备 busy 区间先按小 gap 合并成执行片段，避免把 kernel 间调度抖动当成 step；但达到切步阈值的空档不合并（否则相邻 2ms 空档会被 4ms 合并阈值吞掉）。
- 信号冲突不硬切：保留边界但标 `uncertain`，并在 `notes` 说明。
- 首步起点没有前一边界，`per_step[0].confidence` 记为 0 且 `uncertain=true`；权威模式窗口从首个权威起点开始（前导预热不计为一步），首步按权威边界记 1。

## 输出

- 终端摘要：step 数、总体置信度、自检 F1、每条边界的时间/置信度/信号。
- `--json`：家族统一信封（`mode=single`），额外含：
  - `boundaries[]`：`time_ms / confidence / signals / evidence / uncertain`
  - `self_check`：`authority_count / our_count / matched / precision / recall / f1 / tolerance_ms / max_abs_err_ms`
- `--out-html`：时间轴条、判定指标、step 列表、边界证据、权威自检、降级项。

## 降级路径

| 情况 | 行为 |
|---|---|
| 无设备任务（只有 host 事件） | 退化用 host 忙闲切分（排除 ProfilerStep#），`degraded` 明确标注，置信度最高 medium |
| 无设备空档且无 host 批次 | 用稳定周期的 `Synchronize*` 结束兜底，`degraded` 标注“sync 兜底” |
| 无 sync 事件 | sync 维度不投票，其余信号继续 |
| host 无空闲-恢复批次 | host 维度不投票 |
| 无稳定周期 | period 维度不投票 |
| 无任何边界 | 整窗单步，标 `uncertain`，仍产出 JSON/HTML |
| trace 不可读 | HTML 错误页 + 退出码 3，不静默失败 |

## 口径与限制

- 边界时间取“上一个 step 最后一个设备任务结束处”，与 `STEP_TIME` 的 step 起点实测差通常 < 0.3ms；`self_check` 已给出该 trace 的实际误差。
- 本 skill 不还原 Python 调用栈；step 是无栈 trace 上的结构近似。
- `ProfilerStep#N` 只用于权威自检/`--authority`，不进入 host 忙闲与设备 busy 识别。
- 权威 step 常是 profiler 的粗窗口（含多个 decode 迭代），此时默认迭代粒度切分会让 `boundaries` 多于权威步数，`notes` 会提示；需要粗粒度时用 `--min-step-ms`。
- step 内部的偶发长空闲可能被切成独立 step，报告以 `uncertain`/低置信度体现，不隐藏。
- 输出确定：同一输入、同一参数重复运行结果一致。

## 依赖

- 仅 python3 标准库。
- 可选：`kptools-ascend-profiling-analysis-nostack` 的 `detect_steps.py` 可用于人工对照，本 skill 不自动调用、不强制依赖。

## 参考资料

- [references/method.md](references/method.md) — 信号定义、阈值、融合公式、自检口径与调参建议。

## 开发校验

```bash
python3 -m unittest tests.test_step_split -v
```

合成夹具覆盖：稳定周期、带抖动、step 间长空闲、step 内长空闲（已知限制）、多流交错、无 ProfilerStep/无侧车、ProfilerStep 不改变识别结果、sync 兜底、首步置信度、host-only 置信度降级、权威模式、信封契约。
