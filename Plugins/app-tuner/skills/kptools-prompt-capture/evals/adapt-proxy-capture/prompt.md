---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

使用 kptools-prompt-capture，为父 skill 目录中的 `evals/adapt-proxy-capture/fixtures/proxy.py` 接入采集。把 mock proxy 当作待改实例，在新的临时工作目录中改副本，保留 `--backend-port` 并接收 `--capture-config`。后端用 `tests/mock_proxy.py --backend`。`evals/adapt-proxy-capture/verify.py --candidate <副本>` 可核对普通及 SSE 等价性；另外验证取消收尾。只授权改副本和隔离验证，不更改原文件。
