---
name: sandbox-timeline-svg-render
description: 直接消费 sandbox-stage-latency-analyze 的 analysis.json 与 stage-latency.csv，渲染 Gantt 风格嵌套 timeline.svg 和渲染元数据；适用于已有阶段拓扑的可视化，不读取原始日志、不生成 Excel、不重新分析。
---

# SVG 时序图渲染

## 单点职责

按既有阶段拓扑和 LAYOUT 将平均耗时绘制为嵌套 SVG。子阶段宽度仅在父窗口内等比布局，标签保留真实 `avg_ms`；图宽不可用于跨进程传输耗时推导。

## 可执行入口

```bash
bash scripts/run.sh \
  --analysis OUTPUT_DIR/analysis.json \
  --csv OUTPUT_DIR/stage-latency.csv \
  --output OUTPUT_DIR/timeline.svg \
  --metadata OUTPUT_DIR/timeline-render.json
```

新路径仅使用 Python 标准库，**不导入 openpyxl、不读取 Excel**。共享 `gen_timeline.py` 仍保留旧的位置参数 XLSX 输入以兼容历史调用；本 Skill 的正式契约是 JSON/CSV 直读。

## 输入与输出

- 输入：含 benchmark object 的有效分析 JSON；含 `stage_id/depth/component/stage_name/parent_stage_id/avg_ms` 等列的阶段 CSV。
- 输出：非空 `timeline.svg` 和 `timeline-render.json`；元数据记录 case、输入路径、阶段/条形数量、轴范围和 `excel_dependency: false`。

## 成功与 blocked

- 成功：退出码为 0，至少一个 LAYOUT 阶段具有正耗时，SVG 与 metadata 均完整写入。
- blocked：缺共享脚本退出 2；JSON/CSV 不存在、格式错误、缺列、重复阶段、负/非有限耗时、无可渲染阶段或全零耗时均非零失败，不生成“空图”冒充成功。
- analysis、CSV、SVG 与 metadata 的真实路径必须互不重合（旧 XLSX 输入与 SVG 亦然）；首次建目录或写入前退出 2，保留已有哨兵和输入。
