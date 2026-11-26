# 探针口径

## 本地运行时

| 探针 | 证据与通过条件 | 不能推出的结论 |
| --- | --- | --- |
| `runtime.python` / `runtime.package.*` | 实际导入、完整 `__version__`、单一已验证版本配置 | 不把“能导入”单独当作 ready |
| `runtime.torch_cpu_build` | `torch.version.cuda is None` | 不判断其他 wheel 的上游能力 |
| `runtime.exercised_apis` | 检查 `from_items(override_num_blocks)`、`map_batches` 参数与 `ActorPoolStrategy(size=1)`；对内存 JPEG 执行真实 ResNet18 预处理，要求 `(3,224,224)`、`float32`、有限且两次一致 | 不下载权重，不证明完整 Ray Data 任务或模型推理通过 |
| `resource.*` | Linux、affinity 与当前进程 cgroup/可见祖先 CPU、procfs 与当前进程 cgroup/可见祖先可用内存、报告目录文件系统余量；仅本地启动检查 `/dev/shm` 总/可用量 | 不是容量规划，也不预留资源 |

cgroup 同时解析 `/proc/self/cgroup` 与 `/proc/self/mountinfo`，按 v1/v2 挂载根翻译当前路径，并计入当前目录到可见挂载根的全部限制。资源检查只覆盖 smoke 的 2 CPU、对象存储和进程内存需求。可选现有端点不改变本地运行时门槛，因为 driver 和串行参照仍使用本地包；但远端模式不使用本地 `/dev/shm` 作为必需前置。

## 现有 Ray 端点

只有显式 `--address` 才连接。`auto`、`host:port` 或 `ray://host:port` 均视为已有服务目标；拒绝 `local`，避免检查器意外创建运行时。认证/TLS 由用户通过目标版本支持的安全环境或文件提供。地址不得含 userinfo/query/fragment。

连接后读取 `cluster_resources()`，并 `ray.get` 一个仅计算 `sum(range(8))` 和返回实际 worker 上 Python/Ray/torch/torchvision/NumPy/PyArrow/Pillow 版本的微型任务。只有全部 worker 版本、整数校验和以及 CPU 容量都符合才 ready，缺失或错误版本均 fail closed 并记录。因此 socket-open、Dashboard 可达、driver 包导入或 digest 相同都不是充分条件。该任务不读取业务数据，不创建数据集、主题或数据库记录；它仍是一次经用户显式请求的远端执行。

## 状态合成

每个探针都带 `mandatory`。任一 mandatory `blocked` 使总体 `blocked`；没有 blocked 但存在 mandatory `unknown` 时总体 `unknown`；其余为 `ready`。optional 异常只进入 `warnings`。所有子进程均有有限超时并新建进程组；超时只终止该组。报告使用 `O_EXCL`、0600 权限创建，父目录自动创建，不覆盖旧证据。

`ready` 是“可以尝试指定 smoke”的工程门控，不是生产 SLA、全权限、全数据集、模型精度或性能结论。最终仍需由独立基准技能现场复查并执行真实工作负载。
