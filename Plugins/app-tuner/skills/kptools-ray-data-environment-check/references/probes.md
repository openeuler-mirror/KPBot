# 探针与官方 API 参考

| 探针 | ready 条件 | 证据与限制 |
| --- | --- | --- |
| `linux` / `python` | Linux、Python 3.11 | 当前解释器与系统值 |
| `*_version` | Ray 2.56.0、NumPy 2.1.3、PyArrow 23.0.1 | `importlib.metadata` 的实际安装版本；缺失是 blocked |
| `cpus` | 可见 CPU 上界至少 2 | affinity、OS 数量、可见 cgroup quota；隐藏祖先限制仍未知 |
| `finite_memory` | 可见可用内存至少 256 MiB | `/proc/meminfo` 与可见 cgroup 余量取上界 |
| `free_shm` | `/dev/shm` 总量和空闲量均至少 256 MiB | 与本地 object store 预检口径一致，不证明运行成功 |
| `output` | 输出不存在且父目录可写 | 独占创建避免覆盖 |
| `native_operations` | NumPy `arange*2+1` 经 PyArrow 往返精确 | 实际原生库运算，不是仅导入 |
| `ray_data_api` | 所需可调用对象及参数签名存在 | 只导入与反射，绝不初始化 Ray |

整体状态：任一 mandatory blocked 则 blocked；否则任一 mandatory unknown 则 unknown；全部 ready 才 ready。每探针的状态、mandatory、reason、evidence 均保留，不用汇总分数代替证据。

官方 Ray 文档：

- [`ray.init`](https://docs.ray.io/en/latest/ray-core/api/doc/ray.init.html)：`num_cpus`、`object_store_memory`、`include_dashboard`、`_temp_dir`。
- [`ray.shutdown`](https://docs.ray.io/en/latest/ray-core/api/doc/ray.shutdown.html)：本技能只核对接口，不调用。
- [`ray.data.range`](https://docs.ray.io/en/latest/data/api/doc/ray.data.range.html)：确定性整数输入及 `override_num_blocks`。
- [`Dataset.map_batches`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.map_batches.html)：批大小、批格式和函数参数。
- [`Dataset.materialize`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.materialize.html) 与 [`Dataset.take_all`](https://docs.ray.io/en/latest/data/api/doc/ray.data.Dataset.take_all.html)：执行/物化和读取接口。

版本号以脚本常量为本技能支持契约；官方文档用于 API 语义，不把文档中的示例结果当本机证据。
