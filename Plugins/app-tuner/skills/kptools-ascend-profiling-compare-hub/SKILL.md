---
name: kptools-ascend-profiling-compare-hub
description: |
  NPU profiling 对比体系的统一入口（`kptools-ascend-profiling-*` 工具家族内）：接收 1~2 份
  Ascend/torch_npu profiling，自动调度算子、拷贝、流结构、气泡、融合、调用栈、step 组等专项分析，
  聚合成一份总报告。
  当用户说“对比这两份 profiling / 优化前后差多少 / 瓶颈变了吗 / 全面看一下两份 trace”
  这类**宽泛对比意图**时优先使用本 skill；用户已点名某个专项（只看算子/只看拷贝/只切 step）
  时直接用对应专项 skill；只分析单份 trace 走 kptools-ascend-profiling-analysis(-nostack)。
---

# Profiling 对比编排器（hub）

## 适用范围

- 两份采集的**全量对比**（默认），或按关注点裁剪（`--only` / `--skip`）。
- 单份采集：自动只跑支持单份的章节（stream / bubble / Free 构成 / step 组）。
- hub **不实现任何指标**，只做调度、缺项降级、章节编排与数据来源汇总。

## 命名与归属

本 skill 属于 `kptools-ascend-profiling-*` profiling 工具家族（同 `kptools-ascend-profiling`、
`kptools-ascend-profiling-analysis`、`kptools-ascend-profiling-analysis-nostack`），沿用家族前缀，
不另起 `kpbot-<用途>` 名称：

- `kptools-` 表示 app-tuner 插件内**按职责命名的领域工具 skill**，作用域限于 NPU profiling 分析与对比；
  跨域的顶层产品入口（如 `kpbot-app-tuner`）才使用 `kpbot-` 前缀。
- 本 skill 是 profiling 对比体系的编排入口，不是跨领域主编排器；与 `kpbot-app-tuner` 等顶层入口职责不同。
- 保持 `kptools-` 前缀是为了与既有家族成员及姊妹专项 skill（`...-compare-opstats`、`...-compare-free` 等）
  的引用/路由一致，避免重命名导致跨 skill 路由断裂。

依据静态质量规则 C01 中「供工作流或 Supervisor 调用的内部能力可以按职责命名」及「已证实可发现且调用
一致的历史命名可记为通过」的判定，本 skill 按家族职责命名，命名与调用名称一致。

## 输入

- `base` 必填，`cand` 可选（缺省为单份模式）。
- 每个采集支持目录（`*_ascend_pt` / `ASCEND_PROFILER_OUTPUT`）或裸 `trace_view.json`。
- 子 skill 默认在同级目录查找；可用 `--skills-root` 指定（测试/自定义安装用）。

## 工作流

```bash
python3 scripts/hub.py <base> [<cand>] --out-html report.html --json result.json \
  [--only op_,bubble_] [--skip stack] [--timeout 300] \
  [--base-steps 0,1,2 --cand-steps 5,6,7] [--min-delta-ms 0.05]
```

章节顺序：总览（步耗时/四分类）→ 算子 → 数据搬运 → 流结构 → 气泡 → Free 构成 → Host 下发 → Host 排队 → 融合 → 调用栈 → step 组。

`--only/--skip` 接受完整 skill 名、指标前缀（如 `stack_`）或短名（如 `stack`）；未匹配任何章节的过滤项会在 stderr 与 `notes` 中告警。

`--base-steps/--cand-steps`：pair 模式下同时透传给「总览」（`...-compare`，跨采集分别选 base/cand 的 step 组）和「step 组」章节；注意「step 组」章节固定只分析 base 采集，其 `--cand-steps` 表示 base 内的第二组 step，与总览的跨采集语义不同。单份模式下仅「step 组」章节使用。

## 章节与 provider

| 章节 | skill | 模式 | 指标前缀 |
|---|---|---|---|
| 总览：步耗时与四分类 | `kptools-ascend-profiling-compare` | pair | `step_time_ms` 等 |
| 算子维度 | `...-compare-opstats` | pair | `op_` |
| 数据搬运 | `...-compare-h2d-d2h` | pair | `memcpy_` |
| 流结构 | `kptools-ascend-profiling-stream` | both | `stream_` |
| 设备气泡 | `...-compare-bubble` | both | `bubble_` |
| Free 构成 | `...-compare-free` | both | `free_` |
| Host 下发 | `...-compare-enqueue` | pair | `enqueue_` |
| Host 排队 | `...-compare-dequeue` | pair | `dequeue_` |
| 融合证据 | `...-compare-fusion` | pair | `fusion_` |
| 调用栈 | `...-compare-stack` | pair | `stack_` |
| step 组 | `...-compare-steps` | single（base） | `group_` |

## 降级与故障隔离（硬要求）

| 情况 | 章节状态 | 总报告 |
|---|---|---|
| 子 skill 未安装 | `skipped(not_installed)` | 照出，章节标注原因 |
| 输入模式不匹配 | `skipped(needs_two_captures / needs_step_groups)` | 照出 |
| 用户 `--skip` | `skipped(user_skipped)` | 照出 |
| 子 skill 非零退出 | `failed(rc=N)` | 照出 |
| 子 skill 输出坏 JSON | `failed(JSON 解析失败)` | 照出 |
| 子 skill 不符合 A.1 | `failed(A.1 不合规: …)` | 照出 |
| 超时 | `failed(timeout)` | 照出 |

**任何情况下都必须产出总报告**；章节失败只影响该章节。

## 输出

- 终端章节矩阵 + 结论；`--json` 输出 A.1 信封；`--out-html` 输出总报告。
- 总报告的 `metrics` 与 `per_step` 来自「总览」章节；总览不可用时退化为状态指标 `hub_chapter_ok`（并标注它不是性能指标）。
- `data_quality.degraded` 汇总各章节失败原因；`confidence` 取各章节最低置信度。

## 口径与限制

- hub 不重算指标、不做跨章节推论；章节间的一致性只做提示（步数/来源见各章 `notes`）。
- 未合入的专项 skill 会被标 `not_installed`，不影响其余章节。
- 子报告 HTML 写在 `--workdir`（默认临时目录；相对路径按当前 cwd 解析为绝对路径）；临时目录在进程结束时清理，需要留存请显式指定。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_compare_hub -v
```

## 真实数据实测

vLLM Qwen3-30B-A3B（with-stack，rank149）`nomal` vs `eager`，24 MB / 91 MB trace：
11/11 章节 ok，总耗时约 60 s，总览结论「候选更慢：步耗时 +718.8%（×8.19）」。
章节失败隔离、`--only/--skip`、`--base-steps/--cand-steps`（steps 章使用 base 采集）均已实测。
