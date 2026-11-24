---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture。只分析父 skill 目录下 `evals/analyze-request-distribution/fixtures/seg-window.jsonl` 的请求长度和结构分布。窗口为 Unix 秒 `[1700000000, 1700000002)`，没有 tokenizer 或线上输入。交付请求表及摘要，说明没有 token 计数。
