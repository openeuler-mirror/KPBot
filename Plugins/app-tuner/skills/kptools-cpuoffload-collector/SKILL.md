---
name: kptools-cpuoffload-collector
description: >
  torchtitan-npu CPU offload 内存采集器（L0 外部快照，零侵入）。
  **前置条件：torchtitan-npu 训练必须启用 `--training.enable-cpu-offload`**
  ——否则进程不会有 /dev/devmm_svm 映射，probe 直接报 NO_PINNED_VMA。
  对训练 rank 进程读 /proc：maps 定位 driver pinned VMA（/dev/devmm_svm
  段），pagemap 逐页采物理页 present 位与 PFN，zoneinfo 做 PFN→NUMA
  node 映射。产出 probe.<tag>.json + trace.log（人类可读）。按时间锚点采
  序列：after_init → step_1/2/5 → done。当用户需要采集 CPU offload
  内存、分析 pinned 内存分布、验证 offload 内存是否在正确 node、看每步
  是否重建 slab、采集 devmm_svm 段时触发。分析/报告由
  kptools-cpuoffload-analyzer 对 bundle 目录离线生成。
license: MulanPSL-2.0
compatibility: Linux（NPU 环境验证基线）；**要求目标进程已启用 torchtitan-npu 的 cpu offload（`--training.enable-cpu-offload`）**；/proc 只读，同 uid 免 root；pagemap PFN 需 CAP_SYS_ADMIN（无权限时 node 列降级 unknown，总量仍可用）；python3 >= 3.6 纯 stdlib
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: cpu_offload
  version: "1.1.0"
---

# kptools-cpuoffload-collector — CPU offload 内存采集器

## 前置条件（必读）

**本 skill 只适用于启用了 CPU offload 的 torchtitan-npu 训练进程。**

### 什么是 CPU offload

torchtitan-npu 的 CPU offload 是一个可选特性：把 NPU 侧放不下的
**参数、梯度、optimizer state** 卸载到 CPU 的 **pinned 物理页**（锁页
内存），训练每步在 CPU↔NPU 之间来回搬运。启用后，参数在 CPU 侧
占用可达数十 GiB（取决于模型规模和 offload 配置）。

### 怎么启用

```bash
# torchtitan-npu 启动时加一个参数：
NGPU=8 bash scripts/run_train.sh \
  --module torchtitan.models.deepseek_v3 --config <你的配置> \
  --training.enable-cpu-offload \
  ...其他参数

# 或写入 TOML 配置文件（torchtitan 原生格式）：
# [training]
# enable_cpu_offload = true
```

**注意**：cpu offload 还要求 optimizer 配置兼容——默认 AdamW 即可，
`_NovaSwapAdamW` 会自动包装。其他 optimizer 可能不兼容（见
torchtitan-npu 的 trainer.py 的 `_cpu_offload_optimizer` 容器检查）。

### 如果不启用会怎样

```
$ python3 scripts/probe.py --auto --tag after_init --out /tmp/co
ERROR: NO_PINNED_VMA（未找到 /dev/devmm_svm 段——目标未启用
cpu offload 或驱动未映射；用 --pattern 调整匹配或确认 --tag 时点）
```

**这是预期行为**：`/dev/devmm_svm` 映射**只在 `pin_memory()` 被调用时
才创建**（由 PinnedFlatPool/CpuMemoryPool 的 `_grow` 触发）。普通
torch_npu 初始化（`set_device` + 创建张量）**不会产生** devmm 段——
真机已验证（见"验证"节）。

### 对应的代码路径

启用 `--training.enable-cpu-offload` 后，torchtitan-npu 在
`extensions/torchtitan/trainer.py` 中：

```python
if config.training.enable_cpu_offload:
    # T2: optimizer 容器换成 CpuOffload*（只改 schema，不分配）
    # T3: pool = PinnedFlatPool("fsdp-offload")  ← 零分配
    # T4: with pool.pinned_pin_window():          ← 装 wrapper
    # T5: super().__init__(config)                ← 第一波 pin（param chunk）
    # T6: self._carve_grad_slots(pool)            ← 第二波 pin（梯度 slot）
    # T8: optimizer.step() 首次                    ← 第三波 pin（nova slab）
```

三波 pin 的性质不同——前两波在初始化期一次性完成，第三波**每步可能
重来**（`max_cached_bytes=0` → free 即驱逐）。这是采集要按时间序列
采的根本原因。

## 背景：为什么需要这个 skill

CPU offload 的 pinned 内存走 driver 的 special PTE（无 `struct page`），
**`smaps`/`numastat`/`RSS` 对它全部失明**——只有 `/proc/<pid>/pagemap`
能穿透。当用户问"CPU offload 占了多少内存、在哪个 node、是否每步
都在涨"，常规工具回答不了。本 skill 用 pagemap 采样回答这些问题。

## 三层机制（为什么会有三波 pin）

| 层 | 实现 | 角色说明 |
|---|---|---|
| ① 宿主层 | `PinnedFlatPool`（fsdp）/ `CpuMemoryPool`（nova） | 提供 pinned 物理页 |
| ② 搬运层 | FSDP hook / `swap_primitive` | 执行 H2D/D2H |
| ③ 编排层 | FSDP hook 调度 / `_NovaSwapAdamW` | 决定何时搬/搬到哪 |

另有 `CpuStaging` 的 4 个 bounce slot——毫秒级寿命的中转缓冲，
是已知盲区（外部快照和 hook 都难抓全）。

## 工作流程

```
前置：目标训练已加 --training.enable-cpu-offload
1 bash scripts/discover.sh             # 环境自检
2 等 super().__init__ + _carve_grad_slots 完成后（warmup 前）：
  python3 scripts/probe.py --auto --tag after_init --out <bundle>
3 第 1 步结束后：
  python3 scripts/probe.py --auto --tag step_1 --out <bundle>
4 后续 step_2 / step_5 / done（判断 nova 是否每步重建）
5 bundle 目录交 kptools-cpuoffload-analyzer 出报告
```

## 采集命令

```bash
# 初始化后（warmup 前）：fsdp 两波已完成、nova 还是 0——预期
python3 scripts/probe.py --auto --tag after_init --out /mnt/workspace/co

# 每个 step 后（至少 step 1 / 2 / 5）
python3 scripts/probe.py --auto --tag step_1 --out /mnt/workspace/co

# 指定 PID
python3 scripts/probe.py --pid 12345 --tag step_5 --out /mnt/workspace/co

# 全量逐页精确（1 GiB 约数十秒；默认采样 1/64 页外推）
python3 scripts/probe.py --pid 12345 --tag precise --out /mnt/workspace/co --full
```

## trace.log（人类可读）

```
==== kptools-cpuoffload-collector CPU offload 内存采集日志 ====
口径: /proc maps(/dev/devmm_svm 段) + pagemap present 页计数
     VA 预留 ≠ 驻留——smaps/numastat 对 driver pinned 失明
====================================================================
[10:21:33] 快照 after_init（采样 1/64）：2 rank，pinned present 总计 3.50 GiB
  rank 0        pid=12345  VA 预留   4.00 GiB / present   2.00 GiB / 3 段
       node 分布: node0:80.0% node1:20.0%
[10:21:56] 快照 step_1（采样 1/64）：2 rank，pinned present 总计 4.50 GiB
```

## 输出契约

```
<bundle>/
├── trace.log              # 人类可读采集日志（追加式）
├── probe.after_init.json  # 每 tag 一份：ranks[].{pid,rank,vmas[],total_present_kb,node_kb}
├── probe.step_1.json      # 同上（时间演化序列）
└── ...
```

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--auto` / `--pid <PID>` | 自动发现 rank / 手动指定 | - |
| `--tag <名>` | 快照标签（时间锚点） | 必填 |
| `--out <dir>` | bundle 输出目录 | 必填 |
| `--sample-ratio <N>` | pagemap 采样比 | 64 |
| `--full` | 全量逐页精确模式 | - |
| `--pattern <str>` | VMA 匹配子串 | /dev/devmm_svm |
| `--proc-root <dir>` | procfs 根（测试注入） | /proc |

退出码：`0` 成功；`1` 参数错误；`2` /proc 不可读；`3` 目标不存在 /
无 rank；`4` 未找到 pinned VMA（**目标未启用 cpu offload——检查
`--training.enable-cpu-offload`**）。

## 约束

- 只读：不修改目标进程任何状态
- **前提**：目标进程必须启用 `--training.enable-cpu-offload`（否则
  rc=4 NO_PINNED_VMA）
- 同 uid 免 root；pagemap 的 **PFN 字段需 CAP_SYS_ADMIN**
- `after_init` 快照中 nova-slab 为 **0 是预期行为**（还没发生）
- bounce buffer（CpuStaging 4 slot）毫秒级寿命，已知盲区
- driver 内部为什么选这个 node：用户态不可见

### 和同类 skill 的分界

- 判读/报告由 `kptools-cpuoffload-analyzer` 覆盖
- 线程绑核采集由 `kptools-thread-affinity-collector` 覆盖
- 瞬时 numa_maps 快照由 `kptools-proc-collector --numa` 覆盖

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 4 + NO_PINNED_VMA | **目标未启用 cpu offload**——确认启动命令有 `--training.enable-cpu-offload`；或检查 torchtitan-npu 版本是否支持 |
| PFN 可读性 = false | 无 CAP_SYS_ADMIN；node 列将全为 unknown，总量仍正确 |
| after_init 中 nova 为 0 | 预期（nova 在首个 step 才分配）；继续采 step_1 |
| 采样比例下偏差大 | 换 `--full` 或调小 `--sample-ratio` |

## 可选：L1 埋点注入（需要池归属/调用栈时）

`scripts/sitecustomize_cpuoffload.py` — 零改码 patch 5 个埋点：
`PinnedFlatPool._grow` / `CpuMemoryPool._allocate_slab` /
`Optimizer.step` / `AffinityExtension.start`。用法：

```bash
export PYTHONPATH=/path/to/inject:$PYTHONPATH
export CPUOFFLOAD_HOOK=1
export CPUOFFLOAD_HOOK_OUT=/mnt/workspace/co
# 正常启动训练——模块 import 时自动 patch
```

不需要时不用注入——L0+L2 已能回答"多少、在哪、是否每步涨"。

## 真机验证

以下在 Ascend910 Pod（CANN 9.2.0，aarch64）上验证：

- discover.sh / rank auto 发现 / pfn 可读性检测 / 退出码语义 ✅
- `/dev/devmm_svm` 设备存在 ✅（crw-rw-rw-，CANN 驱动加载后出现）
- torch_npu 初始化（set_device + 创建张量）**不产生** devmm_svm 段 ✅
  ——验证了"只读 pagemap 需要启用 cpu offload 才有数据"的设计假设
- NO_PINNED_VMA 错误路径正确返回 rc=4 ✅

## 参考资料

- 数据契约见 `kptools-cpuoffload-analyzer` 的 `references/data-contract.md`
