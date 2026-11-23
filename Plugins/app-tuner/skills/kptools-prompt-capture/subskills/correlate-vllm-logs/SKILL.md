---
name: correlate-vllm-logs
description: >
  将已有 prompt 分析请求表与 vLLM 日志按请求 ID 对齐，交付匹配覆盖率和可用耗时分布。
---

# vLLM 日志关联

## 操作流程

1. 确认已有 `analyze.py` 生成的 `requests.csv`，以及对应窗口的 P 日志、D 日志或两者。关联键是 proxy 实际发给 vLLM 的请求 ID，不是采集内部 UUID。缺少请求表时可从已有采集分段生成，不需要再次采集。
2. 核对日志是否含兼容的 `TTFT_DEBUG` 行和请求 ID。只传已有侧的日志，可为同一侧传入多个实例文件：

```bash
python3 "$S/scripts/join_vllm_log.py" --requests "$AN/requests.csv" \
  --p-log "$P_LOG" --d-log "$D_LOG" --out "$JOIN"
```

`$S` 为父 skill 目录，`$JOIN` 为新的受限目录。部分旧日志的 Tokenizer 行没有请求 ID，只能统计日志自身分布，不能按请求归因。没有兼容日志时说明缺项，不改动 proxy 或 vLLM。

## 输出

交付 `requests_joined.csv`、`ttft_breakdown.csv`、`tokenizer_lines.csv` 和 `join_report.html`。分别说明请求总数、带 ID 的覆盖情况和 P/D 匹配数；重算与缺失记录单独注明。区分 proxy 首块计时、后端日志耗时及可选离线计算，未匹配请求不计入内部耗时。
