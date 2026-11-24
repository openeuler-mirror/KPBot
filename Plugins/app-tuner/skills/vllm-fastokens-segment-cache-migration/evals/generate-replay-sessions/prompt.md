---
tags: [vllm-fastokens-segment-cache-migration, generate-replay-sessions]
---

离线生成 3 个会话、每会话 4 轮的合成回放，工具输出长度设为 500 字符，随机种子用 17。文件叫 `replay.jsonl`。请确认行数和同一会话内的历史消息是否保留，不用连接服务。
