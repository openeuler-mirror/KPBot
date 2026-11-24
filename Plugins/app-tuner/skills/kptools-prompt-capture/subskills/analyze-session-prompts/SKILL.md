---
name: analyze-session-prompts
description: >
  从已有 prompt 采集分段分析指定或抽样会话的轮次、内容结构和前缀复用，交付逐轮报告。
---

# 多轮 Prompt 会话分析

## 操作流程

1. 选取对应本次采集窗口的 `FORMAT=1` 分段。如果一个目录混有多次采集，直接传本次分段文件，不默认分析整个目录。
2. 用 `--session` 选指定 ID（可用前缀），或用 `--top` 自动选取。自动选取默认至少两条请求；前缀匹配多个 ID 时先核对范围。以下 `$S` 为父 skill 目录：

```bash
python3 "$S/scripts/session_view.py" "$DATA" --out "$SV" --top 3
```

3. 对照逐轮组成、历史复用和新增内容。没有 tokenizer 时使用字符级数据；用 token 比例时按 [指标口径](../../references/capture-and-analysis.md#分析入口) 区分可比较轮次与全部有 token 计数的请求。标明 `fp:` 推断会话、采集缺页及正文覆盖率。

## 输出

交付 `sessions.html`、会话 Markdown/JSON、`session_requests.csv`、`session_messages.csv` 和 `fleet.json`。默认不输出正文、参数或模型输出，明确获准才用 `--include-text`。没有符合条件的会话时，说明是筛选、轮数还是数据不足，不扩大范围补采。
