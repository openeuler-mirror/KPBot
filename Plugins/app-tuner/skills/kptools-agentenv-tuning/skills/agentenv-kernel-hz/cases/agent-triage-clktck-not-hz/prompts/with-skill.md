请使用 agentenv-kernel-hz skill 分析 evidence/ 下的环境证据，
任务：
1. 读取 evidence/ 下的全部证据文件（合成夹具，按真实采集格式组织）。
2. AgentENV 并发恢复路径有控制路径锁等待，getconf CLK_TCK 输出 100，运行内核配置不可读，也没有调度跟踪或恢复压测脚本。用户问：能否用 sysctl 在线修改 HZ？请给出诊断结论与后续验证计划。
3. 将诊断结论写入 opt/report.json，JSON 字段包含：status （analyzed/blocked/not_applicable/validated/inconclusive/reverted）、validation_level（static/environment/functional/performance）、findings、evidence_paths、candidate_actions、missing_inputs、gain_pct（未实测填 null）。
4. 区分只读诊断、功能使能与性能收益；缺少证据时保持 unknown，不要虚构生效或收益。
5. 完成后用一段话说明你依据了哪些证据文件。
