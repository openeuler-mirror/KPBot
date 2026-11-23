---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture，只分析父 skill 目录下 `evals/analyze-session-prompts/fixtures/seg-sessions.jsonl` 的 `fp:fixture` 会话。说明逐轮结构、已有复用证据和正文覆盖情况，写出会话产物，不展示原文。
