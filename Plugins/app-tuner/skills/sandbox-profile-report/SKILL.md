---
name: sandbox-profile-report
description: 直接复用既有 CubeSandbox analysis.json 生成多 sheet Excel、阶段/分批/Host 三类 PNG 和 report-manifest.json；适用于分析完成后的报告交付，不读取原始日志、不调用 analyzer、不重复计算阶段统计。
---

# Excel / PNG 报告

## 单点职责

将已有分析结果格式化交付。报告不是分析入口，任何输入缺失都不得触发隐式重跑。

## 可执行入口

```bash
bash scripts/run.sh \
  --analysis OUTPUT_DIR/analysis.json \
  --output-dir REPORT_DIR \
  [--manifest REPORT_DIR/report-manifest.json]
```

依赖 `python3 + openpyxl + matplotlib + numpy`。输出：

- `timing_report_CASE.xlsx`：Bench总体、分批时延、Cubelet阶段、Shim阶段、详细子阶段、CubeMaster阶段、Host资源；
- `stages_chart_CASE.png`；
- `blocks_timeline_CASE.png`；
- `host_resources_CASE.png`；
- `report-manifest.json`：源分析路径与 SHA-256、`analysis_reused: true`、`analyzer_invoked: false`、各产物大小。

## 成功判据

退出码为 0，七个 sheet 与三类 PNG 均存在且非空，manifest 最后发布。`benchmark_blocks` 和 `host_intervals` 是分批/Host PNG 的必要输入。

## blocked / 兼容行为

- 缺共享实现退出 2；分析 JSON 缺失/损坏、必要数组为空、绘图依赖不可用或任一产物缺失/为空均失败，不能留下成功 manifest。
- analysis、manifest、XLSX 与三类 PNG 的真实路径必须互不重合；首次 `mkdir/unlink/write` 前校验并退出 2，避免删除分析或让 manifest 覆盖产物后伪成功。
- 共享脚本仍接受旧形式 `generate_excel_charts.py PROFILE_DIR [OUTPUT_DIR]`，但现在明确读取 `PROFILE_DIR/analysis.json`；若不存在则 blocked 并提示先运行阶段分析，**不再内部 run_analyze**。
- `export_to_excel.py` 保留为只复用 analysis+CSV 的轻量兼容导出器，不承担完整报告成功契约。
