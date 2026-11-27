# 输入输出契约

## 输入：`benchmark-comparison/v1`

```json
{
  "contract": "benchmark-comparison/v1",
  "run_id": "run-001",
  "counter_quality": "valid",
  "execution_model": "single-threaded",
  "counter_scope": "process-aggregate",
  "work_metric": {"field": "effective_work_units", "unit": "operations"},
  "thresholds": {
    "min_samples_per_cohort": 3,
    "material_change_ratio": 0.10,
    "equivalence_ratio": 0.05
  },
  "samples": [
    {
      "cohort": "fast",
      "elapsed_ms": 100.0,
      "effective_work_units": 1000,
      "task_clock_ms": 95.0,
      "instructions": 300000000,
      "cycles": 300000000
    }
  ]
}
```

所有计数必须属于同一计数范围和同一 benchmark 窗口。顶层必须是 JSON 对象，
`min_samples_per_cohort` 必须是至少 1 的整数。

`counter_quality` 必须显式提供。`valid` 才允许输出 `confirmed`；缺失或 `unavailable` 返回
`blocked`，其他非 `valid` 值返回 `inconclusive`。

`execution_model` 为 `single-threaded` 或 `multi-threaded`；`counter_scope` 为 `critical-path`、
`process-aggregate` 或 `cgroup-aggregate`。多线程样本只有 `critical-path` 能进入确认判定；只有全进程或
cgroup 累计 `task-clock`、instructions 和 cycles 时返回 `inconclusive`。

派生指标按每个样本先计算、再取 cohort 中位数：

- `instructions_per_work = instructions / work`
- `task_clock_ms_per_work = task_clock_ms / work`
- `cpi = cycles / instructions`
- `effective_ghz = cycles / task_clock_ms / 1,000,000`

## 输出：`benchmark-cpu-efficiency-variance-decision/v1`

输出包含 `status`、`confidence`、`evidence` 和 `required_evidence`。状态语义：

- `confirmed`：工作量和单位指令数等价，单位 CPU 时间上升，且 CPI 上升或有效频率下降达到预声明实质变化线。
- `not_supported`：额外工作/指令或 off-CPU 时间更符合数据。
- `inconclusive`：CPU 时间增加但缺少可区分机制，或计数质量不足。
- `blocked`：缺必需计数、样本不足或输入非法。

退出码 0 只表示 decision 已生成；输入格式或路径错误返回 2。
输出路径必须位于脚本启动时的当前工作目录内，父路径不得包含符号链接。无 `--force` 时输出采用原子的不覆盖创建；并发写同一路径只有一个调用成功，其余调用保留已有结果并返回 2。`--force` 只允许替换 contract、detector 和 run_id 都相同的既有 decision，不能覆盖任意文件。
