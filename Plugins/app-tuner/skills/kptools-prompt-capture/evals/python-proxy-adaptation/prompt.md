---
tags: [kptools-prompt-capture]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

使用 kptools-prompt-capture。测试环境由运行者提供一份未接入采集的 Python proxy 源码、启动命令及 mock 后端，仅使用这个指定目标，在运行者提供的工作目录准备接入补丁和隔离测试。我想分析宏观 prompt 分布，并跟踪几个 session；具体数量、全文权限和采集时间还没确定。

当前没有授权替换、重启或采集。不要扫描其他节点、容器，不安装依赖，不改运行路径，不访问真实业务服务。说明你需要我补充的配置、测试结果和部署风险；测试进程必须收尾。源码与启动命令由运行者作为 fixture 附上，缺失时先询问，不猜路径。
