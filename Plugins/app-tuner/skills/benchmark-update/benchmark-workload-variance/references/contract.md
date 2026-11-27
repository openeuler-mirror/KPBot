# 输入输出契约

## 输入：`benchmark-comparison/v1`

```json
{
  "contract": "benchmark-comparison/v1",
  "run_id": "run-001",
  "benchmark_mode": "fixed-work",
  "work_metric": {
    "field": "effective_work_units",
    "unit": "operations",
    "causal_role": "independent"
  },
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
      "instructions": 300000000
    }
  ]
}
```

`cohort` 只能是 `fast` 或 `slow`。数值必须有限且大于 0。`work_metric.field` 指向每个样本中的工作量字段。阈值是采集前确定的实验参数，不由脚本根据结果调节。顶层必须是 JSON 对象，`min_samples_per_cohort` 必须是至少 1 的整数。

`benchmark_mode` 必须是以下值之一：

- `fixed-work`：每轮计划工作量由输入或实验方案独立决定，可以比较实际完成的工作是否偏离计划。
- `fixed-duration`：每轮运行时长固定，完成请求数通常是系统速度的结果；此时必须把这类指标标记为 `outcome`，不能据此确认工作量原因。

`causal_role`：

- `independent`：由输入、到达负载或业务计划决定，可以用于工作量归因。
- `outcome`：由系统速度决定，例如固定时长压测中的完成请求数，不能用于确认工作量原因。

## 输出：`benchmark-workload-variance-decision/v1`

```json
{
  "contract": "benchmark-workload-variance-decision/v1",
  "detector": "benchmark-workload-variance",
  "run_id": "run-001",
  "status": "confirmed",
  "confidence": "high",
  "evidence": {},
  "required_evidence": []
}
```

状态语义：

- `confirmed`：工作量增加且单位工作量成本等价，能解释慢轮。
- `not_supported`：工作量等价，波动需由其他 skill 解释。
- `inconclusive`：指标相互冲突或只解释部分差距。
- `blocked`：缺必需字段、样本不足或工作量字段是结果变量。

脚本退出码 0 表示已生成合法 decision，不表示一定 `confirmed`；格式或路径错误时退出码为 2。
输出路径必须位于脚本启动时的当前工作目录内，父路径不得包含符号链接。无 `--force` 时输出采用原子的不覆盖创建；并发写同一路径只有一个调用成功，其余调用保留已有结果并返回 2。`--force` 只允许替换 contract、detector 和 run_id 都相同的既有 decision，不能覆盖任意文件。
