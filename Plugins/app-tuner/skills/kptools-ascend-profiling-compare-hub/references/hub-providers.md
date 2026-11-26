# hub provider 契约

## 1. 调用方式

每个章节 = 一个 provider，独立子进程：

```
python3 <skills-root>/<skill>/<script> <base> [<cand>] --json <tmp>.json --out-html <tmp>.html
```

- 工作目录固定为子 skill 目录（脚本内的相对资源不受调用方 cwd 影响）。
- 单份 provider（`mode=single`）即使传了两份采集也只吃 `base`，章节备注「使用 base 采集」。
- `--base-steps/--cand-steps` 透传给总览（跨采集分别选 base/cand 的 step 组）和 step 组章节（固定只分析 base）；`--min-delta-ms` 透传给总览/算子/step 组。
- `--workdir` 的相对路径先按 hub 进程 cwd 解析为绝对路径再传给子进程，避免子进程写进 skill 目录。

## 1.1 `--only` / `--skip` 取值

过滤 token 对以下三种写法等价匹配（`--only` 与 `--skip` 规则一致）：

- 完整 skill 名：`kptools-ascend-profiling-compare-stack`
- 指标前缀：`stack_`（以及 `op_`、`memcpy_`、`stream_`、`bubble_`、`free_`、`enqueue_`、`dequeue_`、`fusion_`、`group_`）
- 短名：前缀去掉末尾下划线，如 `stack`、`op`、`bubble`、`free`

因此 `--skip stack` 与 `--skip stack_` 都会跳过调用栈章节。未匹配任何章节的过滤项会在 stderr 告警并写入总报告 `notes`，不会静默 no-op。

## 2. 状态机

| 状态 | 触发 | 计入 degraded |
|---|---|---|
| `ok` | 退出码 0 + JSON 可解析 + A.1 九字段齐全 | 否 |
| `skipped` | not_installed / needs_two_captures / needs_step_groups / user_skipped | 是 |
| `failed` | rc≠0 / JSON 解析失败 / A.1 不合规 / timeout | 是 |

超时默认 300s，`--timeout` 可调；超时只杀该子进程。

## 3. 聚合规则

- `metrics` / `per_step`：只取总览章节（`required=True`），并给每条 metric 的 `evidence` 加 `<skill> / ` 前缀。
- 总览不可用：`metrics=[{name: hub_chapter_ok, verdict: info}]`，`confidence` 至多 `medium`；全部失败为 `low`。
- `sidecars`：各成功章节的并集。
- `confidence`：各成功章节的最低值（high > medium > low）。
- `notes`：章节成功率 + 「hub 不重算指标」声明。

## 4. 章节顺序

总览 → 算子 → 数据搬运 → 流结构 → 气泡 → Free 构成 → Host 下发 → Host 排队 → 融合 → 调用栈 → step 组。
新增 provider 时只允许在表尾追加，避免已有报告章节顺序漂移。

## 5. 与专项 skill 的边界

- hub 不产出任何性能指标，也不从原始 trace 重新计算。
- 用户点名专项时，直接调用对应专项 skill 更快（输出更聚焦、少跑无关章节）。
- 单份 trace 的深度分析不属于本体系，走 `kptools-ascend-profiling-analysis` / `-analysis-nostack`。
