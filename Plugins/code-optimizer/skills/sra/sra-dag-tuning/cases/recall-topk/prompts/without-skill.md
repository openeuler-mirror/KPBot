# without-skill.md — agent 评测提示词（不带 skill，对照组）
你是性能优化工程师。请阅读 src/dag_baseline.cpp，在保持输出 checksum 完全一致的前提下优化其性能（可新建 src/optimized.cpp）。

要求：
1. 优化后用 verify.sh 验证，checksum 必须一致。
2. 说明你做了哪些优化、各自实测收益。
