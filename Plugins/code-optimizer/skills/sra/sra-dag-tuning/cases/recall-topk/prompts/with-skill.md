# with-skill.md — agent 评测提示词（带 skill）
你是性能优化工程师。请阅读 src/dag_baseline.cpp，在保持输出 checksum 完全一致的前提下优化其性能（可新建 src/optimized.cpp）。

要求：
1. 先加载并遵循 sra-dag-tuning skill 的优化方法总表，按"perf/数据定位热点 → 总表选方法 → 使能 → 验证"流程工作。
2. 每项优化用 verify.sh 做 A/B 交替验证，checksum 必须一致。
3. 说明你用了总表中的哪些方法、各自实测收益。
