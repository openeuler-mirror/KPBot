---
name: analyze-request-distribution
description: >
  对已有 prompt 采集分段做宏观分析，交付请求结构、会话规模、缓存与可用时延指标的分布。
---

# Prompt 请求分布分析

## 操作流程

使用已有 `FORMAT=1` 分段（`jsonl/gz`）或采集目录。基础分析只需 Python 3.9 及以上标准库，不需要 proxy 或模型。一个目录有多次采集时，用 `--since` 和 `--until` 限定时间。输出用新的受限目录：

```bash
python3 "$S/scripts/analyze.py" "$DATA" --out "$AN"
```

`$S` 为父 skill 目录。按 [分析说明](../../references/capture-and-analysis.md#分析入口) 选择指标。只有元数据时可以统计规模和长度，正文结构与 token 级特征以可用样本为准。需要离线分词时，传本地 tokenizer 路径及匹配的模板参数；默认不展示预分词片段。

## 输出

- 交付 `requests.csv`、`sessions.csv`、`summary.csv` 和 `report.html`，列明时间窗口、总请求数及各类指标的样本范围。
- 区分 proxy 首块计时、后端返回的 token 或缓存数和离线缓存模拟；首块不一定含首 token。缺失项标注未采到，未匹配的请求不计入对应耗时。
