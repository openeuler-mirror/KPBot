# 调用树 diff 口径

本文件定义 `kptools-ascend-profiling-compare-stack` 的建树、聚合与 diff 规则。

## 1. 索引来源

不解析 `calltree.py` 的文本输出，而是：

1. 用上游 `kptools-ascend-profiling-analysis/scripts/build_index.py` 把
   `trace_view.json` 建成 sqlite（subprocess 调用，允许；不 import）。
2. 直接读索引 `events` 表：`SELECT pid,tid,ts_ns,dur_ns,name FROM events
   WHERE cat='python_function' AND dur_ns>0`。
3. step 边界来自索引 `steps` 表（`id,start_ns,end_ns`）。

索引缓存：`sha1(abs_trace_path|size|mtime)[:16].db`，默认目录
`~/.cache/kpbot_profiling_stack/`。缓存 db 必须能读到 `meta.has_stack` 才复用，否则重建。
超时 900s；失败写 `degraded`，该侧标 `failed`。

`build_index.py` 的查找顺序（兼容两种仓库布局）：

```
skills/kptools-ascend-profiling-analysis/scripts/build_index.py
skills/*/kptools-ascend-profiling-analysis/scripts/build_index.py
skills/*/*/kptools-ascend-profiling-analysis/scripts/build_index.py
```

## 2. 建树

对每个 `(pid, tid)` 分组，按 `(ts_ns ASC, dur_ns DESC)` 排序：

- 先弹出 `end <= 当前 start` 的栈帧（已结束）；
- 从栈顶向下找最近的满足 `end >= 当前 end` 的帧作为父；
- 中间跨边界（`start` 更早但 `end` 更短）的帧保留在 children；self 只扣除直接子区间，不丢弃帧；
- 无父则为根。

同一 (pid,tid) 的多棵根树都参与聚合。

## 3. 折叠机械层

`COLLAPSE_SUBSTR` 复制自上游 `calltree.py`（17 条：`_call_impl`、`torch/_ops.py`、
`<built-in method`、`_record_function_*`、`profiler_interface.py`、`decorate_context`、
`_dispatcher`、`logging/__init__.py`、`logger.py`、`): cast`、`: req_ids`、`: num_reqs`、
`autograd/profiler.py`、`_has_torch_function`、`torch/_tensor.py`、`_get_device_index`）。

折叠语义：

- 被折叠帧**不进入聚合**；
- 其子节点上提到最近的未折叠祖先（或根），深度不变（与 `calltree.py` 输出一致）；
- 被折叠帧自身的非子节点时间归父，不单列。

## 4. self / total

- `total = end − start`（节点自身区间）。
- `self` = 帧区间扣除直接子帧区间后的**独占区间列表**（子帧区间取并集，负值不再出现）；
  `self` 标量 = 这些区间长度之和。该值跨线程累计、包含线程阻塞/被调度出去的等待，
  **不是 CPU 时间**；判断 CPU 占用需线程 CPU 时钟或调度证据。
- 按归一化函数名跨实例求和：`self_sum`, `total_sum`, `count`。
- 逐步统计时，每段独占区间按与各 step 窗口（`[step.start, step.end)`）的**交集**分摊；
  跨步帧不再整体归到起始步，逐步总量、函数统计和噪声带使用同一批独占区间。

`stack_total_self_ms` 是全部函数 `self_sum` 之和；由于折叠后的子节点已上提，
它等于保留帧的总区间时长减去其直接子帧区间并集后的剩余时长之和。

## 5. 名称归一化与行号漂移

Ascend python_function 名称形如 `worker_base.py(345): run` 或
`vllm/worker/worker_base.py(340): execute_model`。默认归一化：

1. 删除字面量 `(file:line)` 后缀：`foo (/a/b.py:12)` → `foo`；
2. 行号漂移容忍：`file.py(345)` → `file.py(*)`、`file.pyc(12)` → `file.pyc(*)`；
3. 剥离目录前缀（绝对与相对）：`/a/b/c.py(*)`、`vllm/worker/c.py(*)` → `c.py(*)`；
4. 压缩连续空白。

`--strict-line` 关闭以上全部规则，保留原始名称。用于验证「行号漂移是否被正确合并」。

- **别名合并**：同一归一化 key 下出现多个原始名称时，`notes` 给出组数。
- **unresolved**：归一化后仍为空、仍含路径分隔符或仍含 `file.py(数字)` 的，归 `unresolved`
  并计数；这部分不参与可信 diff 结论。

## 6. diff 与判定

- 共有函数：`Δ self = self_base − self_cand`（正值为候选更省）。
- `stack_top_self_ms`：在两侧函数名并集（**排除 `unresolved`**）里取 `|Δ self|` 最大者；
  函数名写入 `evidence`。`unresolved` 是归一化失败的混合组，函数身份不可信，只保留其计数与耗时汇总，
  不参与新增/消失/最大变化排名。
- `stack_new_funcs / stack_gone_funcs`：归一化名称集合差；固定 `base=0`，`verdict=info`。
- `stack_changed_funcs`：共有函数中 `|Δ self| > --min-delta-ms` 的数量；固定 `base=0`，
  `verdict=info`（集合差异，不是量的 Δ）。
- 时间类指标噪声带：`max(--min-delta-ms, MAD(逐 step 逐对 Δ))`，逐对 Δ 只在共同 step 上计算；
  `|Δ| ≤ noise` 判 `noise`，否则按 Δ 符号判 `improved/regressed`。全量值仅作描述，
  方向判定用配对样本；`stack_top_self_ms` 的单函数判定采用同一配对口径。
- `per_step`：逐 step 的 `stack_total_self_ms` 按序号配对，步数不一致时较短侧为 `null`。

## 7. 不可比

任一条件成立即输出「不可比」：

- trace 中无 `cat=python_function`；
- 索引中 python_function 事件数为 0；
- 找不到 `build_index.py`；
- 该侧索引构建失败/超时。

此时：

- `summary` 写明「无法比较调用栈 + 原因」；
- `data_quality.confidence=low`，原因进 `degraded`；
- 只给可用一侧的画像（`base`/`cand` 另一侧为 `null`），不做 Δ；
- 退出码 0（输入路径本身不可读才是退出码 3）。

**不允许**退化为 cpu_op 树、kernel 序列或其他代理指标来「凑出」调用栈结论。

## 8. 层级边界

- 本 skill 只统计 host 侧 `python_function` self 时间，不与 device kernel 时间相加；
- 与 `opstats`（device kernel 集合/耗时）是不同层级，结论不能互相替代；
- 跨 rank / 跨采集不做时钟对齐，绝对时间不可直接叠加。
