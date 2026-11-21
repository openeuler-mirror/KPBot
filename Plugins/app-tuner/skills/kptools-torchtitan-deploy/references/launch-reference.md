# 启动参考 — run_train.sh 逐项解析、多机与配置系统

> 事实来源：scripts/run_train.sh、scripts/run_train_multinodes.sh（master）、
> docs/user-guides/quickstart.md 与 developer_guides/config_extensions.md。

## 1. 单机 run_train.sh

### set_env 探测链（脚本头部）

```
ASCEND_SET_ENV_PATH（显式指定，推荐）
  → /usr/local/Ascend/cann/set_env.sh
  → /usr/local/Ascend/ascend-toolkit/set_env.sh
  → /home/developer/Ascend/ascend-toolkit/set_env.sh
```

非标准安装路径用 `ASCEND_SET_ENV_PATH` 指过去，否则环境变量缺失会导致
后续所有 NPU 调用静默走错。

### torchrun 注入的环境变量（逐项）

| 变量 | 值 | 作用 |
|------|-----|------|
| `TORCHINDUCTOR_NPU_BACKEND` | `ascendc` | Inductor 编译后端（AscendC AutoFuse 的入口开关） |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` | NPU 显存分配策略（减少碎片） |
| `CUDA_DEVICE_MAX_CONNECTIONS` | `1` | 限制多流并发依赖（对齐 GPU 语义的兼容开关） |
| `CPU_AFFINITY_CONF` | `1`（可覆盖） | torch_npu CPU 绑核总开关：`0`=关闭（完全惰性）；`1`=按设备切片分核段（mode1）；`2`=官方角色映射（main/acl/release/hccl_watchdog/other 各指定核，`2,force:N` 可强制）。**注意**：lazy_bind（默认）下主线程绑定的唯一机制是 `pt_autograd_*` 线程首次 backward 时代绑——torchtitan 默认关闭 autograd MT（上游 PR #3565），代绑链路永不触发，**主线程静默失效**（acl/release 线程照常绑定，极具迷惑性）。确诊与修复见 pitfalls B-19 |
| `TASK_QUEUE_ENABLE` | `2` | host 侧算子下发队列模式（hostbound 场景关键） |
| `HCCL_CONNECT_TIMEOUT` | `3600`（秒） | HCCL 建连超时——大 EP 建图慢是**预期**，勿调小 |
| `STREAMS_PER_DEVICE` | `32` | 每设备流数 |
| `MULTI_STREAM_MEMORY_RESERVE` | `1` | 多流显存预留 |
| `LOG_RANK` | `0` | `--local-ranks-filter` 只打该 rank 日志；多 rank 排障改它 |

### 默认值与透传

- `MODULE=torchtitan.models.deepseek_v3`、`CONFIG=deepseek_v3_debugmodel`
  ——`--module`/`--config` 可覆盖，其余 CLI 参数原样透传给训练入口
  （`torchtitan_npu.train`）
- `COMM_MODE` 非空 → 跳过 torchrun，`LOCAL_RANK=0 python3 -m` 单进程直跑
  （调试用，如 `COMM_MODE=gloo`）

## 2. 多机 run_train_multinodes.sh

- `NODE_IPS=ip1,ip2,...`：逗号列表，**顺序即 rank 分配**
- 网卡：脚本自动选**第一个非 docker0 的全局 IPv4 网卡**，同时设
  `GLOO_SOCKET_IFNAME` 与 `HCCL_SOCKET_IFNAME`；自定义网络（多网卡/
  网桥命名异常）环境要人工核对选中的网卡
- `HCCL_IF_BASE_PORT=30000`：HCCL 基础端口（多实例同机注意冲突）
- 三个 7200s 防御超时：`HCCL_CONNECT_TIMEOUT` / `HCCL_EXEC_TIMEOUT` /
  `ACL_DEVICE_SYNC_TIMEOUT`——多机大 EP 建连/执行慢是预期，这些值是
  防误杀的，不是性能参数

## 3. 配置系统（2026-05 重构后）

```
启动形态：--module <python模块路径> --config <注册名> [CLI 覆盖...]
```

- 模型选择：`--module torchtitan.models.<model>`（上游模块）
- 配置选择：`--config <name>`（`config_registry` 注册的配置函数，如
  `deepseek_v3_debugmodel`）
- CLI 参数直接覆盖配置函数取值（`--training.local-batch-size` 等）
- **弃用**：`--job.config_file xxx.toml`（旧 TOML 方式，master 不再支持）
- NPU 扩展字段挂载点：
  - 已有上游组：继承扩展（如 `optimizer.<field>`）
  - NPU 新增：`training.extension.*`、`extension.<group>.*`
  （如 `--extension.quantization.enable-quantized-training
  --extension.quantization.recipe all_block_fp8`）
- 配置系统实现：`torchtitan_npu/config/`（configs.py 定义类型 /
  converters.py 按字段拷贝+derive / manager.py patch ConfigManager）

## 4. 并行与硬件门槛

- 沿用 torchtitan 原生 4D 并行（FSDP2/TP/CP/PP）与 EP；`--comm.mode` 配置
- 8 卡起点示例：`examples/deepseek_v4/debug/deepseek_v4_flash_8p_cpt_4k_a3.sh`
  （EP8/DP8 配置可直接改）
- EP 通信与 dense/FSDP 通信分离（专用 HCCL 进程组，`extensions/
  ep_process_group.py`）：新 EP 进程组**必须在 torch.compile 观察前 warm**
- **低精度训练仅 A5（Ascend 950）**：`all_block_fp8` / `all_mxfp8` 等
  recipe + torchao-npu；A3 上开启会直接失败
- 多模态/MTP/Muon 等特性开关见 docs/feature_guides/（17 篇）

## 5. 训练日志与指标（部署验证视角）

torchtitan 逐步输出（rank0 过滤后 stdout）：

```
step/loss/tps/tflops/mfu/elapsed_time_per_step ...
```

- 部署冒烟只看"有输出且 loss 在动"；指标解读/对比/预警归 `train-metrics`
  （其解析器对这些字段原生支持，别名表可扩展新版本格式）
- 排障提示：profiler 采样会让日志混入额外输出，影响指标解析健康度
  （train-metrics 的健康度门会标出）

## 6. TorchFT（容错训练）注意

- `TORCHFT_LIGHTHOUSE` 环境变量启用 lighthouse
- HCCL 进程组重建**必须换新 store prefix**：残留 rendezvous key 会直接
  报错（`extensions/experiment/torchft/process_group.py`）
- HCCL 可能在返回 Work 前阻塞（含建连），设备恢复不自动恢复 HCCL——
  容错恢复链路设计要显式处理这个边界
