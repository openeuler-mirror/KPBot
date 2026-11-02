# 任务：执行一次 RocksDB 调优循环

用 `/workspace/fixtures/mock_db_bench.sh` 做一次真实的调优循环（它只读地模拟 db_bench，指标由参数决定）：

1. 先跑 baseline（默认参数），记录 `stall_count` / `p99_ms` / `throughput_ops`。
2. 严格只修改一个参数（例如 `--max_background_jobs` 或 `--write_buffer_size`），复测一次。
3. 给出 before/after 对比表和"保留/回退"结论，并说明依据。
4. 把完整过程与结果写到 `/workspace/results/report.md`。

约束：

- 每次调用 mock 时设置 `MOCK_LOG=/workspace/results/mock_invocations.log`。
- 不要一次改多个参数。
- 不要对真实数据库做任何破坏性操作。
