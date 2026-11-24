---
name: kptools-cpuoffload-collector
description: >
  torchtitan-npu CPU offload 内存采集器（L0 外部快照，零侵入）。
  **前置条件：torchtitan-npu 训练必须启用 `--training.enable-cpu-offload`**
  ——否则进程不会有 /dev/devmm_svm 映射，probe 直接报 NO_PINNED_VMA。
  对训练 rank 进程读 /proc：maps 定位 driver pinned VMA（/dev/devmm_svm
  段），pagemap 按 present 位统计物理驻留与 PFN→NUMA node 归属，
  zoneinfo 做 PFN→node 映射。产出 probe.<tag>.json + trace.log（人类
  可读）+ run_meta.json（设备亲和基准）。按时间锚点采序列：after_init
  → step_1/2/5 → done。当用户需要在开了 CPU offload 的训练进程上
  **采集** pinned 内存快照（找 rank 进程、落盘 bundle、多时间锚点
  序列、devmm_svm 段定位）时触发。判读/报告由姊妹 skill
  kptools-cpuoffload-analyzer 负责；本 skill 只做采集。
license: MulanPSL-2.0
compatibility: Linux（NPU 环境验证基线）；**要求目标进程已启用 torchtitan-npu 的 cpu offload（`--training.enable-cpu-offload`）**；/proc 只读，同 uid 免 root；pagemap PFN 需 CAP_SYS_ADMIN（无权限时 PFN 屏蔽的页归入 unknown 桶，总量仍可用）；python3 >= 3.6 纯 stdlib
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

CLI 是唯一启用方式（torchtitan-npu master 2026-05 配置系统重构后
不再通过 `--job.config_file` 加载 TOML）：

```bash
NGPU=8 bash scripts/run_train.sh \
  --module torchtitan.models.deepseek_v3 --config <你的配置> \
  --training.enable-cpu-offload \
  ...其他参数
```

详见上游 `docs/feature_guides/cpu_offload.md`。

**注意**：cpu offload 还要求 optimizer schema 兼容——`extensions/
trainer.py` 里 `isinstance(self.optimizers, CpuOffloadOptimizersContainer)`
检查不通过会直接 ValueError（默认 AdamW 可用，`_NovaSwapAdamW` 会
自动包装；其他 optimizer 可能不兼容）。

### 如果不启用会怎样

```
$ python3 scripts/probe.py --auto --tag after_init --out /tmp/co
ERROR: NO_PINNED_VMA（未找到 /dev/devmm_svm 段——目标未启用
cpu offload 或驱动未映射；用 --pattern 调整匹配或确认 --tag 时点）
```

**这是预期行为**：`/dev/devmm_svm` 映射**只在 `pin_memory()` 被调用时
才创建**（由 PinnedFlatPool/CpuMemoryPool 的 `_grow` 触发）。普通
torch_npu 初始化（`set_device` + 创建张量）**不会产生** devmm 段——
真机已验证（见"真机验证"节）。

### 对应的代码路径

启用 `--training.enable-cpu-offload` 后，torchtitan-npu 在
`extensions/trainer.py` 中：

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
都在涨"，常规工具回答不了。本 skill 用 pagemap 采集回答这些问题的
数据面。

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
5 bundle 目录交 kptools-cpuoffload-analyzer（MR !315，评审中）出报告；
  未合入时可按下方「输出契约」人工判读
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
[10:21:33] 快照 after_init（采样 1/64（外推））：2/2 rank，pinned present 总计 3.50 GiB
  时间窗: 0.312s（多 rank 顺序采集，各 rank ts 见 JSON）  PFN 可读: True
  rank 0        pid=12345  VA 预留   4.00 GiB / present   2.00 GiB / 3 段
       node 分布(KB): 1:1.70GiB unknown:0.30GiB
[10:21:56] 快照 step_1（采样 1/64（外推））：2/2 rank，pinned present 总计 4.50 GiB
```

## 输出契约（bundle 目录，本节为判读自足口径）

```
<bundle>/
├── trace.log              # 人类可读采集日志（追加式）
├── run_meta.json          # world_size/device_node_map（首个快照写一次）
├── probe.after_init.json  # 每 tag 一份（同 tag 重跑需 --force）
└── probe.step_1.json      # 同上（时间演化序列）
```

`probe.<tag>.json` 字段（**present/node 全部 KB 口径**，采样模式下为
外推值，每 VMA 带 `sampled.{samples,step}` 证据）：

```
{
  "skill": "kptools-cpuoffload-collector", "version": "1.1.0",
  "ts": <bundle 时间>, "ts_min"/"ts_max"/"duration_s": <多 rank 采集时间窗>,
  "tag": ..., "pfn_readable": <bool>, "sample_ratio": 0(全量)|N(采样),
  "pattern": ..., "expected_ranks": N, "partial": <bool>,
  "missing": [ {pid, rank, reason: process_gone|permission_denied} ],
  "ranks": [
    { "pid": ..., "rank": ..., "ts": <本 rank 采集时刻>,
      "mode": "full"|"sampled_1_N",
      "total_va_kb": <VA 预留>, "total_present_kb": <驻留>,
      "node_kb": { "<node>": <KB>, "unknown": <KB> },   # present 但 PFN 屏蔽/zone 外的页
      "pagemap_unreadable"?: true,                       # 该 rank 只有 VA 大小
      "vmas": [
        { "start", "end", "perms", "pathname", "size_kb",
          "present_kb", "node_kb", "sampled"?: {samples, step} } ] } ]
}
```

`run_meta.json`：`{skill, version, ts, world_size, sys_root,
device_node_map}`——NPU 设备按 PCI **vendor 0x19e5 + class 0x120000
双校验**（单查 vendor 会把 Huawei 网卡误计入），值为
`{ "<pci_addr>": {"node": <numa_node>} }`。

L1 埋点产物（可选注入，见下节）：
`alloc.<pid>.jsonl`（`kind=alloc|copy|free` + `source/method/size/
result_ptr/arg_ptr/tid/caller/dur_us`，result_ptr↔arg_ptr 供配对）、
`marks.<pid>.jsonl`（`hook_installed/..._patched/opt_step_begin/end`）。

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--auto` / `--pid <PID>` | 自动发现 rank / 手动指定（逗号分隔） | - |
| `--tag <名>` | 快照标签（时间锚点） | 必填 |
| `--out <dir>` | bundle 输出目录 | 必填 |
| `--sample-ratio <N>` | pagemap 采样比（每 N 页读 1 页外推） | 64 |
| `--full` | 全量逐页精确模式（1 GiB 数十秒） | - |
| `--pattern <str>` | VMA 匹配子串 | /dev/devmm_svm |
| `--force` | 覆盖已存在的同 tag probe JSON | - |
| `--proc-root <dir>` | procfs 根（测试注入） | /proc |

退出码：`0` 成功；`1` 参数错误（含 `--pid` 非数字、**tag 已存在**、
`--auto` 发现多个作业需改用 `--pid`）；`2` /proc 不可读；`3` 目标不
存在 / 无 rank；`4` 未找到 pinned VMA（**目标未启用 cpu offload——
检查 `--training.enable-cpu-offload`**）；`5` maps/pagemap 无权限
（跨 uid）；`6` 部分 rank 失败（partial，明细在 JSON `missing` 字段，
快照已落盘）。argparse 用法错误同样退 1（不与 2 撞码）。

## 约束

- 只读：不修改目标进程任何状态
- **前提**：目标进程必须启用 `--training.enable-cpu-offload`（否则
  rc=4 NO_PINNED_VMA）
- 同 uid 免 root；pagemap 的 **PFN 字段需 CAP_SYS_ADMIN**（屏蔽的页
  归入 `unknown` 桶，不是丢弃）
- `after_init` 快照中 nova-slab 为 **0 是预期行为**（还没发生）
- bounce buffer（CpuStaging 4 slot）毫秒级寿命，已知盲区
- driver 内部为什么选这个 node：用户态不可见
- 多 rank 顺序采集有时间窗（`duration_s` 披露）；训练活跃期跨 rank
  对比要留意各 rank `ts` 差

### 和同类 skill 的分界

- 判读/报告：`kptools-cpuoffload-analyzer`（MR !315 评审中；未合入
  时按上方「输出契约」节人工判读）
- 线程绑核采集：`kptools-thread-affinity-collector`（MR !247 评审中）
- 瞬时 numa_maps 快照：`kptools-proc-collector`（已合入）

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 4 + NO_PINNED_VMA | **目标未启用 cpu offload**——确认启动命令有 `--training.enable-cpu-offload`；或检查 torchtitan-npu 版本是否支持 |
| 退出码 5 | 跨 uid 读 /proc——用同 uid 或 root 跑 probe |
| 退出码 6 | 部分 rank 探测失败——JSON `missing` 有 pid+原因；已采集部分仍可用 |
| 退出码 1 + MULTI_JOB | 同机多个训练作业都是 rank 0——stderr 列了各作业 pids，改用 `--pid` |
| 退出码 1 + TAG_EXISTS | 同 tag 重跑——换 tag 或加 `--force` |
| PFN 可读性 = false | 无 CAP_SYS_ADMIN；PFN 屏蔽的页归入 unknown 桶，总量仍正确 |
| after_init 中 nova 为 0 | 预期（nova 在首个 step 才分配）；继续采 step_1 |
| 采样比例下偏差大 | 换 `--full` 或调小 `--sample-ratio` |

## 可选：L1 埋点注入（需要池归属/调用栈时）

`scripts/sitecustomize.py` —— **文件名不能改**（Python 启动时自动
import `sitecustomize` 模块）。零改码 patch 4 组埋点：
`PinnedFlatPool._grow/.allocate/.pin_copy`（fsdp chunk）、
`CpuMemoryPool._allocate_slab/.allocate/.free`（nova slab）、
`PinnedCpuStorage.allocate/.free`（owner 生命周期）、
`torch.optim.Optimizer.step`（step 边界 mark）。

```bash
mkdir -p /tmp/cohook
cp scripts/sitecustomize.py /tmp/cohook/      # 文件名保持 sitecustomize.py
export PYTHONPATH=/tmp/cohook:$PYTHONPATH
export CPUOFFLOAD_HOOK=1
export CPUOFFLOAD_HOOK_OUT=/mnt/workspace/co_hooks
# 正常启动训练——目标模块 import 完成后自动 patch
```

不需要时不用注入——L0 快照已能回答"多少、在哪（node 维度）、是否
每步涨"；需要池/调用栈归因时再上 L1。

## 真机验证

以下在 Ascend910 Pod（CANN 9.2.0，aarch64）上验证：

- discover.sh / rank auto 发现 / pfn 可读性检测 / 退出码语义 ✅
- `/dev/devmm_svm` 设备存在 ✅（crw-rw-rw-，CANN 驱动加载后出现）
- torch_npu 初始化（set_device + 创建张量）**不产生** devmm_svm 段 ✅
  ——验证了"只读 pagemap 需要启用 cpu offload 才有数据"的设计假设
- NO_PINNED_VMA 错误路径正确返回 rc=4 ✅

## 参考资料

- 上游 `docs/feature_guides/cpu_offload.md`（启用方式与特性说明）
- 判读口径的完整数据契约：kptools-cpuoffload-analyzer 的
  `references/data-contract.md`（MR !315 评审中；本文件「输出契约」
  节为合入前的自足口径）
