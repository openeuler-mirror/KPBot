---
name: kptools-vllm-ttft-workflow
description: 编排跨两个或更多阶段的 vLLM TTFT 任务，检查打点、采集和报告之间的证据与授权。用于明确要求端到端或跨阶段协调；只有一项任务则用对应的专用 skill。
compatibility: Python 3.10+; requires installed sibling kptools-vllm-ttft (!181), kptools-vllm-ttft-instrumentation (!219), kptools-vllm-ascend-ttft-instrumentation (!221), kptools-vllm-ttft-collect (!222), and kptools-vllm-ttft-report (!224) for the corresponding routes.
---

# TTFT 工作流编排

输入：任务目标、现有日志或部署状态、是否需要两侧打点、输出路径。

先据下表决定路由，再用宿主暴露的 Skill 列表核对该路由所需入口，确认同级基础 CLI 可读；需要 JSON 时也确认采集 `scripts/handoff.py` 可读。只核对选中阶段（已有日志不要求安装打点）。存在缺项时一次列出所有缺失依赖和路径，停止在任何发请求或改源码之前。宿主不提供独立枚举时，使用原生 Skill 加载结果判断是否可用，不用目录存在来断言已注册。

**必须先调用下游 skill，再执行该阶段的命令。** 在 Claude Code 中调用 `Skill` 工具，参数 `skill` 使用实际注册名；通过 app-tuner 插件加载时使用 `app-tuner:<名称>`，单独安装时使用对应的非命名空间名称。在其他宿主使用其原生 skill 加载工具。直接 `Read` 下游的 `SKILL.md`、直接跑 CLI 或仅在总结里提到 skill，都不能替代这一调用。下游 skill 不可用或调用失败时，将该阶段标为阻塞并停止，不绕过调用要求。

| 已有条件 | 下一步 |
| --- | --- |
| 已有日志 | `kptools-vllm-ttft-report`；不发请求、不碰源码。 |
| 服务已就绪、缺日志 | `kptools-vllm-ttft-collect` → 验证 JSON → 报告；不重启。 |
| 明确需要部署打点 | 用 `kptools-vllm-ttft-instrumentation` 和/或 `kptools-vllm-ascend-ttft-instrumentation` 出计划 → 确认应用及服务状态 → 采集 → 报告。 |

两侧计划可独立获取；同部署应用串行，状态目录分开。非必要不改两侧。启停沿用现有部署 SOP，由操作者确认。若只应用一侧成功，停止，不发请求冒充完整采集；列出已改组件和状态路径。回滚是单独授权，不自动执行。旧批准不作本次授权。

让采集 skill 写版本化 JSON，报告前由报告入口用共享 `handoff.py verify` 核对文件哈希及请求。不把打点状态塞进交接 JSON，不依赖聊天记录断言已完成。最终逐阶段列出完成、跳过、阻塞项及证据路径，区分既有日志和本轮请求。
