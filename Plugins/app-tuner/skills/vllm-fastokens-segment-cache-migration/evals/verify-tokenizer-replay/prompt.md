---
tags: [vllm-fastokens-segment-cache-migration, verify-tokenizer-replay]
---

用测试环境已经准备好的多会话回放文件和候选 fastokens 目录，比较当前 HF tokenizers 和候选段缓存开启组。用 64 MiB 段缓存、3 轮、3 个并发线程。交付分词器层的结果，不用重新生成回放、安装包或修改服务。
