---
name: kptools-vllm-ttft-workflow
description: 编排跨两个或更多阶段的 vLLM TTFT 任务，检查打点、采集和报告之间的证据与授权。用于明确要求端到端或跨阶段协调；只有一项任务则用对应的专用 skill。
compatibility: Python 3.10+; requires the sibling kptools-vllm-ttft package from KPBot PR 181 or a compatible installed revision.
---

# TTFT 工作流编排

输入：任务目标、现有日志或部署状态、是否需要两侧打点、输出路径。

**必须先调用下游 skill，再执行该阶段的命令。** 在 Claude Code 中调用 `Skill` 工具，参数 `skill` 使用实际注册名；通过 app-tuner 插件加载时使用 `app-tuner:<名称>`，单独安装时使用对应的非命名空间名称。在其他宿主使用其原生 skill 加载工具。直接 `Read` 下游的 `SKILL.md`、直接跑 CLI 或仅在总结里提到 skill，都不能替代这一调用。下游 skill 不可用或调用失败时，将该阶段标为阻塞并停止，不绕过调用要求。

| 已有条件 | 下一步 |
| --- | --- |
| 已有日志 | `kptools-vllm-ttft-report`；不发请求、不碰源码。 |
| 服务已就绪、缺日志 | `kptools-vllm-ttft-collect` → 验证 JSON → 报告；不重启。 |
| 明确需要部署打点 | 用 `kptools-vllm-ttft-instrumentation` 和/或 `kptools-vllm-ascend-ttft-instrumentation` 出计划 → 确认应用及服务状态 → 采集 → 报告。 |

两侧计划可独立获取；同部署应用串行，状态目录分开。非必要不改两侧。启停沿用现有部署 SOP，由操作者确认。若只应用一侧成功，停止，不发请求冒充完整采集；列出已改组件和状态路径。回滚是单独授权，不自动执行。旧批准不作本次授权。

让采集 skill 写版本化 JSON，报告前由报告入口用共享 `handoff.py verify` 核对文件哈希及请求。不把打点状态塞进交接 JSON，不依赖聊天记录断言已完成。最终逐阶段列出完成、跳过、阻塞项及证据路径，区分既有日志和本轮请求。
