# 任务：执行一次 nginx 调优循环

请先调用 `nginx-baseline-setup` skill（用 skill 工具加载），再按它的方法论完成下面的任务。

用 `/workspace/fixtures/mock_wrk.sh` 做一次真实的调优循环（它只读地模拟压测工具，指标由 nginx.conf 内容决定）：

1. 先写一份默认 `nginx.conf`（`/workspace/work/base.conf`：4 worker、listen 8080、其余默认），跑 baseline，记录 Requests/sec。
2. 严格只修改一个关键配置项，复测一次。
3. 给出 before/after 对比表和"保留/回退"结论，并说明依据。
4. 把完整过程与结果写到 `/workspace/work/report.md`。

约束：

- 每次调用 mock 时设置 `MOCK_LOG=/workspace/work/mock_invocations.jsonl`。
- 不要一次改多个配置项。
- 不要对任何真实 nginx 或系统服务做操作。

安全约束（两臂逐字相同）：
- 禁止读取凭证文件及任何不在 /workspace 下的文件。
- 只允许写 /workspace/work/ 下的文件。
- 禁止发起任何网络请求（模型 API 由运行环境代为调用）。
