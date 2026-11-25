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
| `CPU_AFFINITY_CONF` | `1`（可覆盖） | torch_npu CPU 绑核总开关：`0`=关闭（完全惰性）；`1`=按设备切片分核段（mode1）；`2`=官方角色映射（main/acl/release/hccl_watchdog/other 各指定核，`2,force:N` 可强制）。**注意**：lazy_bind（默认）下主线程绑定的唯一机制是 `pt_autograd_*` 线程首次 backward 时代绑——torchtitan 默认关闭 autograd MT（上游 PR #3565），代绑链路永不触发，**主线程静默失效**（acl/release 线程照常绑定，极具迷惑性）。确诊与修复见 pitfalls B-18 |
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
  （调试用，如 `COMM_MODE=fake_backend`——上游 run_train.sh 注释标注的
  "config validation without NPU" 干跑；comm.mode 合法值为
  default/fake_backend/local_tensor/torchcomms）

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
- 多模态/MTP/Muon 等特性开关见 docs/feature_guides/ 目录（篇数随上游演进）

## 5. 训练日志与指标（部署验证视角）

torchtitan 逐步输出（rank0 过滤后 stdout）：

```
step/loss/tps/tflops/mfu/elapsed_time_per_step ...
```

- 部署冒烟只看"有输出且 loss 在动"；指标解读/对比/预警由所在部署环境的
  训练指标工具承接（对上述字段原生解析的工具通常支持别名表扩展新格式）
- 排障提示：profiler 采样会让日志混入额外输出，影响指标解析健康度
  （有健康度门的指标工具会标出这类污染）

## 6. TorchFT（容错训练）注意

- `TORCHFT_LIGHTHOUSE` 环境变量启用 lighthouse
- HCCL 进程组重建**必须换新 store prefix**：残留 rendezvous key 会直接
  报错（`torchtitan_npu/extensions/torchtitan/experiments/torchft/process_group.py`）
- HCCL 可能在返回 Work 前阻塞（含建连），设备恢复不自动恢复 HCCL——
  容错恢复链路设计要显式处理这个边界

## 7. affinity 扩展配置（MR 1100，已合入 master）

> 状态：**已随上游 [MR 1100](https://gitcode.com/cann/torchtitan-npu/merge_requests/1100)
> 合入 master（2026-11-02），master 直接可用**。可**规避**（非根治）
> pitfalls B-18 的主线程静默失效。

### 7.1 配置入口与总开关

配置入口仍是 `CPU_AFFINITY_CONF`（torch_npu 与扩展共用，**二者正交**）：
官方参数原样透传给 torch_npu，扩展只新增 `turbo:` 子选项的识别——

| 写法 | 含义 |
|------|------|
| `CPU_AFFINITY_CONF=1` 或 `2`（无 turbo） | 扩展**完全不介入**，官方语义原样生效（零动作零干扰） |
| `1,turbo:auto` | assist 模式 + 自动探测频率分层 |
| `2,turbo:0-7` 或 `turbo:0,1,2` | full 模式 + 手工指定高频核集合 |
| `...,turbo:off` | 显式关闭扩展（等价默认） |

turbo 子项就是**扩展总开关**；其余官方子选项（`mode:N`/`npu_affine`/
`lazy_bind`/`force`/`bind_irq`/`npu<N>:<range>`）全部留给 torch_npu 解析。

### 7.2 模式语义（mode × turbo 组合）

| CONF | 行为 |
|------|------|
| `1,turbo:*`（assist） | main 绑本 rank 设备切片（按卡均分的连续核段），turbo 仅做激活开关 |
| `2,turbo:*`（full） | 拓扑探测 → rank 隔离核规划 → 保留线程绑定（main > acl > release > hccl_watchdog 优先级，turbo 高频核优先关键线程）→ daemon 周期兜底 |

### 7.3 挂载点与五模块（master 实现）

- **挂载**：`torchtitan_npu/extensions/torchtitan/trainer.py` —— Trainer
  init 尾部 `AffinityExtension.start()`，训练收尾 `affinity.stop()`
- **五模块**（`torchtitan_npu/extensions/affinity/`）：
  `__init__`（CONF 解析+编排）→ `topology`（4 层探测：cgroup cpuset →
  DCMI → sysfs PCI → npu-smi）→ `planner`（rank 跨步互斥域 + 频率排序）→
  `binder`（官方 API → 重绑保留线程 → 掩码读回验证）→ `daemon`
  （掩码状态分类救援；周期轮询；断路器）
- **永不影响训练**：任何亲和异常只记日志并自禁用，绝不抛出

### 7.4 验证

- 单测：`python3 -m pytest tests/unit_tests/extensions/affinity/ -q`
  （用例数随上游演进，以仓库现状为准）
- 功能核对方法（**验收绑核以掩码为准，不以 psr 为准**）：
  - 先锁定**训练 PID**（torchrun 主进程下的 rank 进程，不是 launcher），
    读 `/proc/<训练pid>/task/*/status` 的 `Cpus_allowed_list` 周期采样，
    核对各线程掩码终态与配置语义一致——**这是"绑核是否生效"的判据**
  - `ps -L -o tid,comm,psr` 的 psr 只显示"线程最近运行的 CPU"：
    **未绑核的线程也可能连续多次落在同一核**——psr 只用于观察实际落点
    分布/漫游，不能当绑核成功的验收证据
  - 门控正确性——无 turbo 时各线程掩码应与官方语义**逐位一致**且日志
    输出 `extension disabled (turbo off; official semantics only)`
