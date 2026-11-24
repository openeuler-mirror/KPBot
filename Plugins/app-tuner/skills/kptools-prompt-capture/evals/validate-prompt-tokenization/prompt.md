---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture。核验父 skill 目录下 `evals/validate-prompt-tokenization/fixtures/gold.jsonl`，使用同目录的 `tokenizer/`。运行环境须预装 `transformers`；不要联网或安装依赖。报告比对统计，不展示正文。
