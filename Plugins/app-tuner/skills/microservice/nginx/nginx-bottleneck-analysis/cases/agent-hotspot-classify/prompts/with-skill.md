# 任务：瓶颈判定与分级路由

请先调用 `nginx-bottleneck-analysis` skill（用 skill 工具加载），再按它的判定树完成下面的任务。

用 `/workspace/fixtures/scenario-a.txt` 与 `/workspace/fixtures/scenario-b.txt` 中的证据，对两个场景分别：

1. 判定瓶颈类别（判定树编号 + 名称），引用量化阈值说明判定依据。
2. 给出分级路由（应进入哪个 skill：配置级/环境级/加速级/回基线修布局）。

把两个场景的判定与路由写到 `/workspace/results/report.md`（结构化：场景、证据摘要、量化依据、判定、路由）。

约束：只做判定与路由，不要执行任何变更或对系统做任何操作。
