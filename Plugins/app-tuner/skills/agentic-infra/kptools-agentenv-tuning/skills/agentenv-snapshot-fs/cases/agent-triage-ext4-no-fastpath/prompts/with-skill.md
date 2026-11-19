请使用 agentenv-snapshot-fs skill 分析 evidence/ 下的环境证据，
任务：
1. 读取 evidence/ 下的全部证据文件（合成夹具，按真实采集格式组织）。
2. AgentENV 快照创建慢：fsbench pwrite 50MB 在 @16KB 与 @4KB 吞吐几乎相同（~21ms），kallsyms 查不到 ext4_iomap_buffered_write_ops，work_dir 位于根文件系统（ext4）。用户问：能否通过 ext4 挂载参数或 journal 调优开启 openEuler 那样的快路径？请给出诊断结论与修复方案。
3. 将诊断结论写入 opt/report.json，JSON 字段包含：status （analyzed/blocked/not_applicable/validated/inconclusive/reverted）、validation_level（static/environment/functional/performance）、findings、evidence_paths、candidate_actions、missing_inputs、gain_pct（未实测填 null）。
4. 区分只读诊断、功能使能与性能收益；缺少证据时保持 unknown，不要虚构生效或收益。
5. 完成后用一段话说明你依据了哪些证据文件。
