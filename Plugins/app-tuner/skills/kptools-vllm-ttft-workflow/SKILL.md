---
name: kptools-vllm-ttft-workflow
description: 编排跨两个或更多阶段的 vLLM TTFT 任务，检查打点、采集和报告之间的证据与授权。用于明确要求端到端或跨阶段协调，也包括从已有日志离线生成交接 JSON 和 HTML 两项产物；只有一项任务则用对应的专用 skill。
compatibility: Python 3.10+; requires installed sibling kptools-vllm-ttft (!181), kptools-vllm-ttft-instrumentation (!219), kptools-vllm-ascend-ttft-instrumentation (!221), kptools-vllm-ttft-collect (!222), and kptools-vllm-ttft-report (!224) for the corresponding routes.
---

# TTFT 工作流编排

输入：任务目标、现有日志或部署状态、是否需要两侧打点、输出路径。

先按下表选择本轮所需阶段，再预检选中阶段，不发请求、不改源码、不启停服务。核对所需下游 Skill 入口（依下列加载规则）；核对所需 Python 3.10+ 及已安装的同级脚本：打点或报告需要基础 `kptools-vllm-ttft/scripts/ttft.py`，采集需要基础 `kptools-vllm-ttft/scripts/report.py` 提供 `list_requests`，采集或 JSON 交接报告需要 `kptools-vllm-ttft-collect/scripts/handoff.py`。仅从原始日志做报告不要求打点或采集入口，也不要求交接脚本；仅用既有 JSON 做报告不需要加载采集入口。一次列出预检发现的缺失依赖及对应路径，并在进入执行前停止。

**每个选中阶段都必须先通过宿主支持的方式加载对应的下游 Skill，再按其指令执行。**

- Claude Code 必须调用原生 `Skill` 工具，`skill` 参数使用实际注册名（如通过 app-tuner 插件注册的 `app-tuner:kptools-vllm-ttft-report`，或单独注册的 `kptools-vllm-ttft-report`）。其他有原生 Skill 加载工具的宿主也必须用其原生工具和实际注册名。有注册列表时据此核对入口；原生宿主不提供单独枚举时，以原生加载结果为准，不凭目录推断。
- 仅当宿主以文件提供已注册的 Skill 且没有原生加载工具时，才从宿主提供的注册列表按名称找到唯一的对应入口，读取该条目指向的 `SKILL.md`，核对其 frontmatter `name` 与所选下游 Skill 的名称一致，再遵循全文指令。没有注册列表、入口缺失或不唯一、读取失败或身份不符，均视为未加载。
- 原生调用不可用或失败时不得改用文件读取。任意搜索后读取、直接运行下游命令或仅在总结提到 Skill 都不算加载。选中入口未成功加载时标记阻塞并停止，不绕过。

按优先级匹配下表；只安排当前目标需要的阶段，明确只出计划时在计划后停止。选择路由不等于获得执行授权。

| 当前目标与证据 | 下一步 |
| --- | --- |
| 明确要求本轮新打点（即使已有日志） | 选对应的 `kptools-vllm-ttft-instrumentation` 和/或 `kptools-vllm-ascend-ttft-instrumentation` 出计划 → 如需应用，取得本轮授权并由操作者确认目标服务已停止后应用 → 针对新打点的采集须等所需打点全部应用成功、服务恢复就绪 → 如需报告，使用满足本轮目标的证据。 |
| 未要求新打点，但明确要求本轮新采集（即使已有日志） | 确认服务已就绪后走 `kptools-vllm-ttft-collect` → 如需报告，交给报告入口处理本轮 JSON；不打点、不重启。 |
| 未要求新打点或新采集，指定了既有 Prefill 日志和完整请求 ID，且要求同时生成交接 JSON 和 HTML | 走 `kptools-vllm-ttft-collect` 的 `existing_logs` 路径，生成交接 JSON → 把交接单及同一份已授权的日志快照交给 `kptools-vllm-ttft-report`；不发请求、不碰源码、不启停服务。 |
| 未要求新打点或新采集，只需 HTML，且既有日志或 JSON 满足本轮目标 | `kptools-vllm-ttft-report`；不发请求、不碰源码。 |
| 未要求新打点或新采集，但没有满足本轮目标的证据 | 如果确需采集且服务已就绪，走采集 → 如需报告，交给报告入口处理本轮 JSON；否则说明证据缺口并停止。 |

新打点或新采集时保留既有日志作为独立对比证据；不可覆盖、混作本轮数据，也不可用它们替代缺失或失败的本轮证据。任何实时采集均须本轮明确授权后才可发请求。

离线生成两项产物时，只读用户授权的 Prefill 日志，按采集入口的要求固定包含目标请求完整关联记录且只有一条 BREAKDOWN 的快照；不能确定请求或边界则停止，不转为实时采集。用完整请求 ID 和 `existing_logs` 从快照写出新的 JSON；成功后将 JSON 和这份已授权的快照路径（不是仍可能变化的原日志）交给报告入口。报告入口按下述流程用该快照的逐字节副本校验、检查并生成 HTML。成功或失败结束时，按授权清理本轮临时快照，保留原日志和已生成的用户产物；如按用户要求保留快照，说明其路径。

两侧计划可独立获取；同部署应用串行，状态目录分开。非必要不改两侧。启停沿用现有部署 SOP，由操作者确认。若只应用一侧成功，停止，不发请求冒充完整采集；列出已改组件和状态路径。回滚是单独授权，不自动执行。旧批准不作本次授权。

让采集 Skill 写版本化 JSON。用 JSON 出报告时，将交接单和用户明确授权用于本轮报告的日志路径交给 `kptools-vllm-ttft-report`；若无明确授权的日志路径则停止，不从 JSON 自动选择路径。由报告入口按其交接流程核验（向 `handoff.py verify` 显式提供 `--log`，使用授权日志的快照）；核验失败则不生成报告。编排阶段不重复核验，不把打点状态塞进交接 JSON，也不依赖聊天记录断言已完成。最终逐阶段列出完成、跳过、阻塞项及证据路径，区分既有日志和本轮请求。
