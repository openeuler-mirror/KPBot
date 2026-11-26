---
name: kptools-ascend-profiling-compare
description: |
  对比两份 Ascend/torch_npu profiling（优化前后、配置/版本差异、或同负载两次采集），
  输出步耗时、四分类、算子/通信/Host API 的变化与瓶颈迁移，并区分“实质变化”和“数据波动”。
  默认按 step 对 step 对齐，也支持指定任意两个/两组 step；只有 trace_view.json（含无栈）也能出 HTML 报告。
  当用户提供两份 profiling、询问“优化前后差多少 / 瓶颈变了吗 / 哪一步变慢 / 两份 trace 对比”时使用。
  只分析单份 trace 时不要用本 skill（走 kptools-ascend-profiling-analysis / -analysis-nostack）；
  融合、stream、气泡、算子、调用栈等专项对比走对应专项 skill，通用入口归 compare-hub（规划中）。
---

# Ascend Profiling 对比（瓶颈变化）

## 适用

- 同一负载的两份采集：优化前后、两个配置、两个版本、两次重复采集（判断波动）。
- 默认对齐对比；也支持 `--base-step` / `--cand-step` 指定任意两步，或 `--base-steps` / `--cand-steps` 指定两组。
- 有栈 / 无栈 trace 都支持；只有 `trace_view.json` 时也必须产出 HTML 报告。

不适用：跨模型、跨业务的性能结论；这类输入只能做数据质量对比，不能当优化收益。

## 触发边界

| 用户意图 | 应走的 skill |
|---|---|
| 两份 profiling 整体比：步耗时/四分类/算子/通信/API/瓶颈迁移 | **本 skill** |
| 只分析一份 trace（有栈或无栈） | `kptools-ascend-profiling-analysis` / `-analysis-nostack` |
| 专项对比：调用栈 diff / stream 结构 / 气泡 / 算子统计 / 融合 / enqueue | 对应专项 skill |
| 不确定该用哪个对比能力、想一次出总报告 | `kptools-ascend-profiling-compare-hub`（规划中） |

## 输入

每个 capture 支持：

1. `*_ascend_pt` 目录或 `ASCEND_PROFILER_OUTPUT` 目录（优先原生侧车）。
2. 裸 `trace_view.json`（同目录侧车自动带上）。
3. 直接给 `analysis.db` / `ascend_pytorch_profiler_*.db`。

原生侧车优先读取：

| 文件 | 用途 |
|---|---|
| `analysis.db:StepTraceTime` | 每步 Computing / Comm(未重叠) / Comm / Free / Stage / Preparing（CANN 权威） |
| `step_trace_time.csv` | 同上，DB 缺失时兜底 |
| `ascend_pytorch_profiler_*.db` | `STEP_TIME` 绝对 step 边界（本 skill 只读取该表） |
| `kernel_details.csv` | 每 step 每 kernel 名称/类型/耗时 |
| `communication.json` / `analysis.db:CommAnalyzerTime` | 通信 elapse/wait/transit/sync（communication.json 优先，缺失时 analysis.db:CommAnalyzerTime 兜底） |
| `api_detail.csv` / `api_statistic.csv` | Host API 聚合 |
| `profiler_info_*.json` | torch_npu/CANN 版本、rank、with_stack |

只有 `trace_view.json` 时：复用 `kptools-ascend-profiling-analysis` 的 `build_index.py` 建索引，复用
`kptools-ascend-profiling-analysis-nostack` 的 `detect_steps.py` 切 step；四分类缺失时按设备任务类型自建。

## 安装与权限

- **安装**：无需安装。仅依赖 python3 标准库与同仓已合入的分析 skill 脚本（`build_index.py` / `detect_steps.py` / `nostack_analysis.py`），克隆仓库后直接运行 `python3 scripts/compare.py ...`。
- **权限**：对输入**只读**（`*_ascend_pt` / `ASCEND_PROFILER_OUTPUT` / `trace_view.json` 及其侧车），不修改被测采集、不写被测目录；输出只写 `--out-html` / `--json` 指定路径；纯 trace 路径的索引缓存写入 `~/.cache/kpbot_profiling_compare/`（按「路径+大小+mtime + build_index 指纹 + schema 版本」哈希命名，自动只保留最近 32 份，可随时删除）。

## 工作流

1. 确认两份采集来自同一负载（模型/版本/rank 至少记录到报告）。
2. 运行：

```bash
python3 scripts/compare.py <base> <cand> --out-html report.html [--json result.json]
```

3. 读报告首页：步耗时 Δ、改善/恶化/噪声判定、瓶颈迁移、主要变化分量。
4. 需要深挖时指定 step 或 step 组：

```bash
# 任意两步
python3 scripts/compare.py <base> <cand> --base-step 3 --cand-step 7 --out-html step_compare.html
# 两组 step（逗号分隔）
python3 scripts/compare.py <base> <cand> --base-steps 1,2 --cand-steps 1,2 --out-html group.html
# 跳过 warmup
python3 scripts/compare.py <base> <cand> --skip-warmup 2 --out-html report.html
```

## 对齐与判定

- **两份采集之间**：默认按 step 位置配对（N ↔ N），禁止按时间戳或排序配对；步数不一致取交集并告警。
- **单份采集内**：直接使用 trace 内同一 epoch 的 host/device `ts`，不做跨时钟平移；不额外读取 host→device 配对表，也不做 host/device 时钟校准。
- **步耗时口径**：`步耗时 = StepTraceTime.stage + preparing`（设备活跃窗口 + 准备期），实测与 `STEP_TIME` 的 step 跨度一致（误差 < 1%）；纯 trace 路径用 step 边界跨度。
- **Δ = base − cand**，正值表示候选更省；报告同时给 `cand/base` 倍率，避免回归时出现 >100% 的百分比误读。
- **噪声判定**：先按 step 配对求逐对 Δ，`noise = max(--min-delta-ms, --min-delta-pct×base, MAD(逐对 Δ))`；`|median(Δ)| ≤ noise` 判“在噪声内”。逐对差消除了 workload 自身的步间方差，少量步也能识别一致性改善。
- **方向一致性**：若 ≥2 对 step 的变化方向不一致（同向占比 < 75%），判“方向不一致”，不下改善/退化或瓶颈迁移结论，并提示按 prefill/decode 或阶段分组。
- 瓶颈迁移：只有步耗时判定为改善/恶化且主导分量改变时才报告迁移。
- 分量可比性：两侧四分类来源跨族（CANN 侧车 vs trace 自建）时，只比较步耗时，Computing/Comm/Free/Preparing 仅展示数值不判定。

## 输出

- 终端摘要 + `--json` 机器可读 + `--out-html` HTML（验收以 HTML 为准）。
- HTML 包含：数据来源/版本、降级说明、步耗时 hero、分量对比（含 cand/base 倍率）、瓶颈迁移、逐步配对、Top 变快/变慢算子、仅单侧出现的算子、通信、Host API、口径与限制。
- 算子名已做实例归一化：`hcom_allReduce__503_0_1` 这类每次采集都变的下标会被折叠成 `hcom_allReduce`；只在单侧出现的算子单独成节，不参与快慢排名（避免把命名漂移当成回归）。

## 降级路径（必须始终有报告）

1. 原生侧车缺失 → `trace_view.json` 路径。
2. 无 `ProfilerStep#N` / 无栈 → 设备 sync / idle gap 切 step。
3. 完全切不出 step → 整窗对比，并显著标注“无可靠 step 边界”。
4. 数据不可读 → 仍输出 HTML 错误页并返回退出码 3。

## 输出契约

`--json` 输出统一 JSON 信封 v1。字段与判定口径的权威定义见
[references/compare-methodology.md](references/compare-methodology.md) 的 §7，
可执行校验为 `scripts/envelope.py::validate`（也可 `python3 scripts/envelope.py result.json`）：

```json
{
  "skill": "kptools-ascend-profiling-compare",
  "schema_version": 1,
  "mode": "pair",
  "inputs": {"base": "...", "cand": "...", "steps": "..."},
  "data_quality": {"sidecars": ["..."], "degraded": ["..."], "confidence": "high | medium | low"},
  "metrics": [{"name": "step_time_ms", "unit": "ms", "base": 12.3, "cand": 11.1,
               "delta": 1.2, "delta_pct": 9.8, "verdict": "improved",
               "noise_band": 0.4, "paired_delta": 1.2, "sign_consistency": 1.0,
               "evidence": "step_source: ..."}],
  "per_step": [{"step": 0, "cand_step": 0, "base": 12.3, "cand": 11.1, "delta": 1.2}],
  "summary": "候选更快：步耗时 9.8%",
  "notes": ["噪声判定: ..."]
}
```

- `metrics[].verdict` 取值 `improved / regressed / noise / info`；`delta = median(base) − median(cand)`，正值表示候选更省；verdict 由逐对 Δ 的中位数 `paired_delta` 判定，`sign_consistency` 为同向步占比；步间方向不一致时给 `info`。
- `delta_pct = Δ / base_median × 100`（回归时会大于 100%，因此报告正文用倍率展示）。
- `data_quality.sidecars` 只列实际读取成功的侧车文件名；`degraded` 逐条说明降级原因；`confidence` 规则：双侧原生侧车、无降级、步数 ≥ 3 且步间方向一致 = `high`；trace-only / 有降级 / 步数 < 3 = `medium`；整窗对比或步耗时方向不一致 = `low`。
- `scripts/envelope.py::validate` 按 methodology §7 校验必填字段、枚举、类型和 `delta/delta_pct` 自洽性；hub 只消费 `metrics / summary / data_quality`。

## 输出一致性

- HTML 与 JSON 由**同一个内部结果对象**渲染，数值同源，不做二次采样或重算。
- 核对方法：`result.json` 中 `step_time_ms` 的 `base/cand/delta` 应与 HTML「分量对比」表的「步耗时」行一致；`per_step` 的配对数量应与「逐步配对」表行数一致。
- 两侧都基于同一份 `sidecars` 读取结果；若 HTML 与 JSON 不一致，属于 bug，按契约测试回归。

## 供其他 skill 复用

`scripts/kpc_core.py` 是与 CLI 解耦的公共读取与度量层（侧车解析、trace 索引、区间运算、MAD、`METRIC_DEFS`、`_metric_block`、`load_capture`）。
对比体系其余 skill 在**本 skill 合入 master 后**可相对路径引用：`from kpc_core import load_capture, _metric_block`（需先把本目录加入 `sys.path`）。合入前一律复制，不得跨目录引用。

## 依赖

- `kptools-ascend-profiling-analysis/scripts/build_index.py`（trace 索引）
- `kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py`（无栈/侧车 step 边界）
- `kptools-ascend-profiling-analysis-nostack/scripts/nostack_analysis.py`（四分类自建）

插件安装时这些 skill 同批分发；缺失时 compare 会降级并在报告里说明。

## 限制

- 不还原真实 Python 调用栈；算子/阶段级变化是聚合结论。
- 跨采集不做时钟对齐，跨 rank 绝对时间不可直接叠加。
- 步间方向不一致（典型是 prefill/decode 混合）时只给“方向不一致”，需要按阶段分组再比。
- 四分类来源跨族（CANN 侧车 vs trace 自建）时只有步耗时可比，分量拆分不可直接对比。
- 任一侧取不到设备任务分类（`class_source=none`）时同样只比步耗时，分量仅展示数值。
- 两份采集的 workload 是否真的相同需要使用者确认；工具只做一致性告警。

## 参考资料

- [references/compare-methodology.md](references/compare-methodology.md) — 对齐、噪声判定、降级路径与报告口径。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_compare -v
```

契约测试覆盖：§7 信封字段与枚举、`sidecars/degraded/confidence`、`--json` 端到端、`kpc_core` 独立可导入。
