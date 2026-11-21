# torchtitan-npu 踩坑实录 — 症状 / 根因 / 证据位置

> 两组证据来源：
> **A 组 = 源码补丁即证据**——仓内 `torchtitan_npu/patches/` 的每个补丁都是
> 一个上游缺陷/限制的实测记录（补丁存在本身说明"不这样就会踩坑"）；
> **B 组 = 实战环境坑**——部署与排障实测，含 cannlab 分设环境特有项。
> 每条给出证据位置，可溯源核查。**发现新坑请追加到本文。**

## A. 源码补丁证据组（升级/换版本时重点复查）

### A-1 全图编译报 `DT_INT64 not supported` 中止

- **症状**：torch.compile + Inductor（AscendC 后端）融合 IndirectLoad 时
  dtype 推断报 DT_INT64 中止全图编译
- **根因**：torch_npu 的 gather_lowering 把 int64 注册进
  gather/embedding/index_select 白名单，AscendC 推断不支持
- **补丁**：`patches/torch_npu/gather_lowering_dtypes.py` 重注册为仅
  int32（int64 走 eager fallback）
- **排障含义**：补丁随包安装自动生效；若报错复现，第一嫌疑是版本配套
  错位导致补丁没装上或被新版本 torch_npu 改动绕过

### A-2 Inductor roofline 估算触发 Triton CUDA 驱动探测崩溃

- **症状**：NPU 上 benchmarking/估算路径尝试加载 CUDA driver 报错
- **根因**：`get_gpu_dram_gbps` 走了 GPU 探测路径
- **补丁**：`patches/torch_npu/inductor_runtime_estimation.py` 用固定
  1200 GB/s 兜底替代

### A-3 异步 DeviceCopy（D2H）不同步，host 读到脏数据

- **根因**：PyTorch 只对 GPU predicate 认可的设备标 pinned，NPU 不在内，
  异步 D2H 无事件同步
- **补丁**：`patches/workaround/device_copy.py` 把 `prims.device_put`
  留在 AscendC 图内、仅对异步 D2H 结果标 pinned，codegen 在 host 读取前
  发同步。副作用防护：不标全量 pinned 是为避免其他 Inductor 路径误选
  Triton

### A-4 FlexAttention / block-mask 编译路径异常

- **补丁**：`patches/workaround/eager_flex.py` 让 FlexAttention 与
  block-mask 创建在 NPU 走 eager 路径（import 即生效）——编译路径不可用
  时的官方降级

### A-5 `aten::_assert_async` 缺 NPU kernel

- **补丁**：`patches/workaround/assert_async.py` 注册 PrivateUse1 空实现

### A-6 确定性训练只开 torch 开关不够

- **症状**：`torch.use_deterministic_algorithms(True)` 后结果仍不确定
- **根因**：NPU 确定性还需要 **`HCCL_DETERMINISTIC=true` +
  `CLOSE_MATMUL_K_SHIFT=1`** 两个环境变量
- **补丁**：`patches/torch_npu/determinism.py` 包装该 API 按开关同步
  设置/清除环境变量——手工设环境变量时要知道这两个名字

### A-7 CPU offload 下 DTensor 初始化驱动 CPU collectives

- **根因**：参数 shard 在 CPU、mesh 在 NPU，上游 `_init_param` 直接
  初始化会错误创建 CPU 集合通信
- **补丁**：`patches/torch_npu/cpu_dtensor_init.py`（仅
  `--training.enable-cpu-offload` 时由 TrainerEx 显式安装）

### A-8 FSDP 逐参数混合精度

- **根因**：composable FSDP 的 `MixedPrecisionPolicy` 只支持整组单 dtype，
  DeepSeek-V4 需个别参数保 FP32
- **补丁**：`patches/torch/distributed/fsdp/`——按 FQN 的
  `policy_overrides`（pytorch issue #156784 的下游 wrap，**针对
  torch 2.14.0.dev 特定文件**：升级 torch 必查此补丁兼容性）

### A-9 EP 进程组必须先 warm 再进 torch.compile

- **根因**：EP 专用 HCCL 进程组在 compile 观察之后创建会被图固化排除
- **实现**：`extensions/ep_process_group.py`（EP 轴复制到专用组，DeepEP
  buffer 独立组，避免 patch dispatcher）

### A-10 TorchFT HCCL 重建的 store prefix 残留

- **症状**：容错恢复重建 HCCL 进程组直接报错
- **根因**：旧 rendezvous key 残留；重建**必须换新 store prefix**

## B. 实战环境组

### B-11 配置系统重构：旧 TOML 用法全部失效（2026-05）

master 弃用 `--job.config_file`，改 `--module` + `--config`。症状：
旧脚本/旧文档照抄报"unrecognized arguments"。这是**排障第一优先核对项**
（比重装环境更快出结论）。

### B-12 版本配套错位（最泛化的坑）

CANN 9.0 环境 + master 分支、python 3.11 + torch 2.14 等混搭会出现
千奇百怪的 import/算子/补丁失效。**报错形态不指向版本本身**（表现为
随机算子缺失/编译中止），所以永远先核版本表（SKILL.md 首表）再排其他。

### B-13 NNAL 安装顺序

装 NNAL 前必须 `source /usr/local/Ascend/cann/set_env.sh`；否则安装脚本
找不到 CANN 组件。对应官方 installation.md 的显式提示。

### B-14 多机网卡选择

HCCL/GLOO 网卡选错（如 docker0）→ 建连卡死/超时。脚本已自动跳过
docker0；自定义网络环境人工核对 `HCCL_SOCKET_IFNAME`。多实例同机注意
`HCCL_IF_BASE_PORT` 冲突。

### B-15 HCCL 超时是防御值不是性能参数

单机 3600s/多机 7200s 是给大 EP 建连留的余量。看到"超时"先查是不是
真的有节点故障/网卡问题，而不是先把超时调小（会引入 flaky）。

### B-16 910B3 sync-start 块数硬件地板（cannlab 实战）

- **症状**：首 dispatch/warmup **瞬间** fatal（21008/507018/ERR99999）
- **根因**：910B3 只有 **20 个 sync-start 块**，要求 >20 块同步启动的
  kernel 无解，属硬件代次地板（不是配置问题）
- **确诊**：设 `ASCEND_PROCESS_LOG_PATH` 后在 device-N/*.log 找
  `require_sync_start block_num=N > limit=20`
- **对照**：数十秒后才报的是看门狗误杀（预热慢被 ~30s 超时杀），两者
  处理完全不同——见 `kptools-cannlab-env` pitfalls #11

### B-17 构建 python 的世界观差异（cannlab 实战）

在自写采样/绑核/诊断脚本时：构建版 python 的 `Thread.ident` 返回
pthread 句柄而非内核 TID、无 `os.gettid`、ctypes 不可用——线程级操作用
`sched_setaffinity(0)` 自绑语义与 `/proc/thread-self`。详见
`kptools-cannlab-env` pitfalls（#5/#6 及相关）。

### B-18 Pod 环境系统层缺失（cannlab 实战）

容器化 NPU Pod 常见：`/dev/shm` 默认 64MB（需 `sudo -n mount -o
remount,size=40G /dev/shm`，重启失效）、`/usr/lib64/aicpu_kernels/`
不存在（依赖 AICPU 预装目录的路径会失败，需 mkdir）。见
`kptools-cannlab-env` pitfalls #9/#10。

### B-19 CPU_AFFINITY_CONF 主线程静默失效（torchtitan 默认编排，重磅）

- **症状**：主线程亲和性全程全核漫游（如 `{0-39}`），**无报错、无告警日志**；
  同时 acl_thread→核1、release_thread→核2 照常自绑定——观测到"绑核生效了"
  实为只有保留线程绑了，**主线程漏绑**（torchtitan 真实训练 100 步周期采样
  + Chrome trace 证实：默认配置下 837 个算子事件全落在未绑定的主线程）
- **根因**（torch_npu 2.14.0.dev20260918，NPUAffinityController.cpp 实测）：
  lazy_bind（默认）下主线程**不自绑**（`StartMainThreadBind` 中主线程直接
  return，L564-567），唯一绑定路径是 `pt_autograd_[0-9]+` 线程在首次
  backward 时代绑（L571 正则门控；该设计为防主线程过早自绑、子线程继承
  窄掩码，见 Ascend/pytorch#2224）。而 **torchtitan 上游 PR #3565** 为配合
  spmd_types/TLS DeviceMesh 默认调用 `_disable_autograd_multithreading()`
  ——backward 在主线程内联执行，`pt_autograd_*` 线程从不 dispatch 设备
  算子 → 代绑链路永不触发。**run_train.sh 默认 `CPU_AFFINITY_CONF=1`
  即受影响**
- **确诊**：
  - plog：`ASCEND_GLOBAL_LOG_LEVEL=0` 后查 `"set main_thread affinity"`
    记录条数（失效时全程 **0 条**；正常时有
    `StartMainThreadBind:584 ... thread name: pt_autograd_0`）
  - 线程 dump（或直接用 `kptools-affinity-trace` 采样）：主线程
    `Cpus_allowed_list` 全核 + `pt_autograd_*` 线程全核浮动即中招
  - 最小复现：`torch.autograd.set_multithreading_enabled(False)` 开关
    对照（同脚本双臂）
- **替代路径也不可用**：eager 模式（`lazy_bind:0`）虽能绑主线程，但多
  rank 下存在掩码继承污染/主线程互撞单核（Ascend/pytorch#5554）——关闭
  autograd MT 的编排下两条路径均失效
- **修复**：`fix-main-bind-universal.patch`（NPUAffinityController.cpp
  单文件 +89/-2：主线程限频探测 `/proc/self/task/*/comm` 中 pt_autograd
  存在性——引擎设备线程首次 backward 时创建、与 MT 开关无关——命中后
  自绑；已在 2×910/CANN 9.2.0 节点构建 wheel 验证：mode1/2 正确绑定、
  mode0 完全惰性、MT-ON 原路径无回归、dataloader worker 无继承污染）；
  跟踪 Ascend/pytorch **#5555**
- **验证脚本设计坑**：探针限频 1 次/秒——最小复现脚本步间需 ≥2s 延时，
  否则 3 步 <1s 跑完探针来不及触发（修复验证时实测踩过）
