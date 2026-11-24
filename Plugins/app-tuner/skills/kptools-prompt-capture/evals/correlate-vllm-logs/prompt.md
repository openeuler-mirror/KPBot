---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture。将父 skill 目录下 `evals/correlate-vllm-logs/fixtures/requests.csv` 与同目录 `p.ttft` 关联。没有 D 日志，只做离线关联，报告匹配覆盖率及无 ID 行的处理。
