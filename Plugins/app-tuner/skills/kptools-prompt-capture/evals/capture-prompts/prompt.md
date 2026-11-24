---
tags: [kptools-prompt-capture]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

使用 kptools-prompt-capture。先在父 skill 目录以 `python3 evals/capture-prompts/fixture.py --serve <新的临时目录>` 启动回环 mock，读取输出中的配置文件、采集目录和端口。已授权在这个 fixture 上限时采集 10 秒，默认仅元数据；窗口内和到期后用 `python3 evals/capture-prompts/fixture.py --request <该端口>` 各发一个合成请求。检查 worker 状态、确认写盘完成并结束本次 fixture，不重启或修改其他实例。
