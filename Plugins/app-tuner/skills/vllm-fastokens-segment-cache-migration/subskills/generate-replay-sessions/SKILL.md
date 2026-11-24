---
name: generate-replay-sessions
description: >
  生成参数可复现的合成多轮会话 JSONL，作为 fastokens 分词回放的输入。
---

# 生成多轮回放数据

## 操作流程

使用本地 Python 3.9 及以上版本，无需连接 vLLM 服务。按验证场景确定会话数、每会话轮数、工具输出长度（字符）和种子。默认值分别为 8、12、6000 和 0。会话数与轮数应大于零；要观察历史复用，应使用多轮会话。`$SESSIONS` 是输出文件路径，其所在目录应已存在。

```bash
"$PY" "$S/scripts/fastokens_segment_cache.py" make-sessions --out "$SESSIONS" \
    --sessions 8 --turns 12 --block-chars 6000 --seed 0
```

每行是一条请求，包含 `session` 和 `messages`。每条请求包含截至该轮的完整对话；生成器每隔一轮在 user 消息中加入合成工具输出。核对请求数等于会话数乘轮数，同一会话的早期消息在后续请求中保留。已有符合格式的回放文件可以直接交给验证步骤。

## 输出

交付 JSONL 路径、生成参数和行数。两组回放使用同一份文件，不采集线上 prompt。
