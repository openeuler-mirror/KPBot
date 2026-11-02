请使用 agentenv-cluster-tlbi skill 分析 evidence/ 下的环境证据，
任务：
1. 读取 evidence/ 下的全部证据文件（合成夹具，按真实采集格式组织）。
2. 目标进程的 cpuset 已绑定到一个 cluster（taskset 验证通过），拓扑里有多个 socket、部分 CPU cluster_id 数值相同、die_id 文件缺失；没有 DVMBM/TLBI 实现与运行时生效证据。用户问：能否把本次 TLBI 优化记为通过？请给出诊断结论。
3. 将诊断结论写入 opt/report.json，JSON 字段包含：status （analyzed/blocked/not_applicable/validated/inconclusive/reverted）、validation_level（static/environment/functional/performance）、findings、evidence_paths、candidate_actions、missing_inputs、gain_pct（未实测填 null）。
4. 区分只读诊断、功能使能与性能收益；缺少证据时保持 unknown，不要虚构生效或收益。
5. 完成后用一段话说明你依据了哪些证据文件。
