---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture，只检查当前 Python proxy 是否支持 prompt 采集。以父 skill 目录为根，`evals/inspect-proxy-capture/fixtures/start.json` 是指定实例的启动记录，源码及采集模块的路径均在其中。请沿启动参数核对实际入口，给出结论和证据位置，不启动进程、发送请求或改造。
