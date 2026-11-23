# 测量与比较契约（schema_version: 1）

## 命令记录

`record_run.py --out <不存在的目录> --timeout <秒> -- <argv...>` 执行一条用户给定的命令，继承当前工作目录与环境，不经过 shell。生成 `stdout.log`、`stderr.log`、`run.json`，记录 UTC 开始时间、墙钟秒数、退出码、超时和计时范围。目录必须新建，启动失败也保留元数据；非零退出/超时不能成为有效性能样本。运行目录以属主专用 `0700` 创建，三个产物文件以 `0600` 创建，即使 umask 较宽松也不会放宽；日志可能含敏感内容，分享前仍需脱敏。

计时从两个日志文件就绪后、即将 `Popen` 时开始，到子进程退出（含超时/中断终止）为止；日志打开失败或 `run.json` 收尾写入不计入 `elapsed_seconds`，避免证据目录 I/O 污染命令耗时。

超时在 POSIX 上终止该命令的进程组；Windows 使用子进程树终止。远端任务、脱离进程组的服务不在本地回收保证内；该工具适合前台、可终止的 benchmark 命令，不应用来启停共享服务。Ctrl-C 同样尝试清理。运行前按当前负载选择超时，不自动重复长测。

工具不解析业务输出、不验证正确性、不采集持续 CPU/GPU 指标。吞吐分母是用户约定的完整命令范围，不能悄悄替换成内部计算时间。多轮结果由用户 harness 或 Agent 读取原始记录后整理成下面的比较输入；各样本的有效性及 evidence 来源不能编造。

## 比较输入

下面是格式示例，数值为合成值；`config` 的实际内容由用户负载决定。

```json
{
  "schema_version": 1,
  "metric": {"name": "throughput", "unit": "rows/s", "direction": "higher"},
  "context": {
    "workload": "generated-table-etl",
    "input_id": "generator-v1-seed-7-10000-rows",
    "scope": "command_start_to_sink_complete",
    "resources": {"cpu_limit": 4, "memory_bytes": 8589934592},
    "software": {"ray": "record-actual-version", "python": "record-actual-version"},
    "cache_state": "warm",
    "config": {"batch_size": 128, "concurrency": 2},
    "correctness_contract": "same-keyed-output-and-schema"
  },
  "samples": [
    {"value": 100, "success": true, "correct": true, "evidence": "run-1/run.json + correctness-1.json"},
    {"value": 102, "success": true, "correct": true, "evidence": "run-2/run.json + correctness-2.json"},
    {"value": 101, "success": true, "correct": true, "evidence": "run-3/run.json + correctness-3.json"}
  ]
}
```

`context` 八个字段均必需；空值、未知值（unknown/unspecified）、缺失条件会阻止收益计算。可增加字段，所有字段默认必须完全相同。输入身份涵盖实际输入规模、分布/seed 或数据指纹；资源涵盖实际 CPU/内存/GPU、节点拓扑与隔离范围；software 记录依赖、代码 revision 等影响比较的版本。结构验证不能证明记录真实或完整，Agent 必须人工核对。

候选只改变明确实验因素时，用 `--allow-change config`（或 `software`）显式允许该顶层字段不同，并在报告里保留前后值。资源、数据身份、测量范围、缓存或正确性契约不同不接受为同条件收益。不要把所有变化都塞进 config，借此绕过检查。

所有样本须为有限正数、`success=true`、`correct=true` 且附 evidence 定位。缺失正确性属于未知，不默认为通过。工具不自动删除离群值、失败轮次或“挑最快”样本；一组混入失败运行时整个比较无效。

`compare_runs.py baseline.json candidate.json --out new.json` 输出原始样本、样本数、median/min/max、样本 CV（单样本为 null）、变化因素及不可比较原因。少于三轮会附提示；三轮是工具提示，不代表统计显著性门槛。

- 高者优指标：`improvement_percent = (candidate_median / baseline_median - 1) * 100`。
- 低者优指标：`improvement_percent = (1 - candidate_median / baseline_median) * 100`。
- `speedup` 分别为 candidate/baseline 或 baseline/candidate；不把 throughput 提升百分比当成耗时下降百分比。
- 不同名称、单位、方向不自动换算。可比性检查失败返回码 2，JSON 中收益为 null；输入格式无效返回码 1。

这些描述性统计不证明因果或显著性。正式判断仍需原始证据、配对/交错顺序、缓存与后台负载控制，并按任务需要增加重复次数。
