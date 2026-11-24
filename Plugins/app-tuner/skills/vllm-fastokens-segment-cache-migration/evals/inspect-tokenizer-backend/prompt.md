---
tags: [vllm-fastokens-segment-cache-migration, inspect-tokenizer-backend]
---

只核对这个 vLLM 服务现在实际用哪个分词后端、有没有配置段缓存。服务 Python、API PID 和 tokenizer 路径由测试环境提供。使用离线模式，把结果写进 `probe.json`。如果拿不到运行中进程的证据，请说明查到了什么，不要继续安装或回放。
