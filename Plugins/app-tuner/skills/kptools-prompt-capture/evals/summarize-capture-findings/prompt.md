---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write]
---

使用 kptools-prompt-capture，只汇总父 skill 目录下 `evals/summarize-capture-findings/fixtures/` 的 `run.json` 和 `summary.csv`。没有原始分段或日志关联产物。请在新的输出目录写 `REPORT.md`，不执行新的采集或恢复。
