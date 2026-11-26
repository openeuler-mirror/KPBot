# agent 级规则判定 fixture（D1 决策：只沉淀，不自动化）

用途：后续 evals 体系的输入——每个条目 = 真实证据 + 期望结论（SKILL.md 规则的可执行判例）。
自动化时机：evals 基建就绪后接入；在此之前禁止将规则复制进脚本（双源漂移）。

| 条目 | 规则来源 | 数据出处 |
|---|---|---|
| cpu-affinity-fine-grained-gate.json | cpu-affinity SKILL.md"Thread Scheduling Interference"阈值表 | e2e torchtitan session（2026-11-17） |
| os-governor-skip-rule.json | os-optimization"governor==performance → skip"规则 | subskill-ab-comparison os 段（2026-11-02 真机） |
