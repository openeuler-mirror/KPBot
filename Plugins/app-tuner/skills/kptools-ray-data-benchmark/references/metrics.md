# 计时、正确性与官方 API 参考

## 指标

- `phases[].wall_seconds`：`time.perf_counter()` 包围每阶段唯一一次 `Dataset.materialize()` 的墙钟。
- `phases[].rows_per_second`：该阶段实际行数除以原始墙钟；warmup 也保留原始记录。
- `summary`：仅 measured 阶段的轮数、总墙钟、逐轮 rows/s 的算术平均和正确性汇总，不混入 warmup。
- `initialization_wall_seconds`：独立记录 runtime 初始化，不进入阶段测量。
- `id_sum/value_sum`：实际输出行求和；与解析期望 `n(n-1)/2`、`n²` 比较。字典比对负责精确 ID/value，行数负责 count。

`range`、UDF 构造与 schedule 配置不计时，但 `materialize` 内实际调度、任务执行和数据物化属于测量范围。`take_all` 正确性读取不计时；UDF marker 在读取前后必须一致，因此没有第二次被测执行。微基准不推出集群容量、扩展性或不同实现的 speedup。

## 生命周期

父进程用 `start_new_session=True` 创建唯一 worker session，并执行硬墙计时。worker 核对请求输出目录、配置中的绝对输出目录和配置文件所在父目录一致，且以独占方式创建 `result.json`。正常/异常路径均在 `finally` 关闭自己初始化的 runtime，shutdown 失败会令最终状态和退出码同时失败；超时或 SIGINT 只向该 session 的进程组发 TERM/KILL。Ray 临时目录使用短路径系统临时目录并在 finally 清理（避免 AF_UNIX 路径上限），日志、配置、runtime control 与失败 JSON 均在新输出目录留存。

预检的 CPU 上界取 affinity、OS 数量和当前进程及其可见祖先的 cgroup v1/v2 quota 最小值，内存同时计入相同可见层级的剩余额度。解析 `/proc/self/cgroup` 与 `/proc/self/mountinfo`，不会越过 cgroup namespace 的挂载根。

## 官方 Ray 文档

- [`ray.init`](https://docs.ray.io/en/latest/ray-core/api/doc/ray.init.html)：本地逻辑 CPU、object store 与临时目录配置。
- [`ray.shutdown`](https://docs.ray.io/en/latest/ray-core/api/doc/ray.shutdown.html)：关闭由当前进程初始化的 runtime；脚本从不调用全局 CLI `ray stop`。
- [`ray.data.range`](https://docs.ray.io/en/latest/data/api/doc/ray.data.range.html)：生成确定性整数 Dataset，`override_num_blocks` 固定并行块数。
- [`Dataset.map_batches`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.map_batches.html)：`batch_format="numpy"` 下返回 NumPy 列。
- [`Dataset.materialize`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.materialize.html)：执行并物化 Dataset，是本技能的计时边界。
- [`Dataset.take_all`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.take_all.html)：仅在物化后读取小规模结果做正确性校验。

所有吞吐值都直接来自当次原始观测；参考文档不提供也不预设任何性能分数。
