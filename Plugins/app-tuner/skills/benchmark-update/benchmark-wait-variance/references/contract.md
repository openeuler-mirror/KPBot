# 输入输出契约

## 输入：`benchmark-comparison/v1`

```json
{
  "contract": "benchmark-comparison/v1",
  "run_id": "run-001",
  "execution_model": "single-threaded",
  "work_metric": {"field": "effective_work_units", "unit": "operations"},
  "thresholds": {
    "min_samples_per_cohort": 3,
    "material_change_ratio": 0.10,
    "equivalence_ratio": 0.05,
    "min_explained_fraction": 0.70
  },
  "samples": [
    {
      "cohort": "fast",
      "elapsed_ms": 100.0,
      "effective_work_units": 1000,
      "instructions": 300000000,
      "task_clock_ms": 90.0,
      "blocked_ms": 5.0
    }
  ]
}
```

`execution_model` 只能是 `single-threaded` 或 `multi-threaded`。单线程使用 `task_clock_ms` 作为关键路径 CPU 时间；多线程必须逐样本提供 `critical_path_cpu_ms`，不能用全进程累计 `task_clock_ms` 代替。顶层必须是 JSON 对象，`min_samples_per_cohort` 必须是至少 1 的整数。

可选等待细分字段为 `runnable_delay_ms`、`blocked_ms`、`throttled_ms`、`io_wait_ms` 和 `downstream_wait_ms`。这些值必须是 benchmark 窗口内、同一关键路径或可归属范围的增量。

派生指标：

- `non_running_ms = elapsed_ms - critical_path_cpu_ms`
- `extra_wait_explained_fraction = (slow_non_running - fast_non_running) / (slow_elapsed - fast_elapsed)`

## 输出：`benchmark-wait-variance-decision/v1`

- `confirmed`：工作量、单位指令数和关键路径 CPU 成本等价，新增 non-running 时间超过实质变化线，并解释了预声明比例的墙钟差。
- `not_supported`：CPU 执行成本或额外工作更能解释差距。
- `inconclusive`：确认存在 non-running 增量，但解释比例或等待归属证据不足。
- `blocked`：缺关键路径 CPU 时间、样本不足或输入非法。

`wait_class` 取贡献最大的已提供细分字段；没有可归属细分时为 `unattributed`。退出码 0 表示 decision 已生成，格式或路径错误返回 2。
关键路径 CPU 成本的等价判断使用差值绝对值；显著下降与等待增加同时出现时返回 `inconclusive` 并标记混合变化。
输出路径必须位于脚本启动时的当前工作目录内，父路径不得包含符号链接。无 `--force` 时输出采用原子的不覆盖创建；并发写同一路径只有一个调用成功，其余调用保留已有结果并返回 2。`--force` 只允许替换 contract、detector 和 run_id 都相同的既有 decision，不能覆盖任意文件。
