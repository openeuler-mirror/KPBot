---
name: kptools-cpuoffload-analyzer
description: >
  CPU offload 内存分析器：读取 kptools-cpuoffload-collector 产出的
  bundle 目录（L0 probe 快照序列，每 tag 一份 JSON + 可选 L1 alloc/
  marks 埋点），执行三条自检（node 映射自洽 / L1 存活配对交叉核对 /
  采样比例放大一致），自动判定：总量时间演化、nova slab 是否每步
  重建（按共同 rank 对齐）、内存 node 亲和、pinned 骤降（候选解释：
  空 slab 驱逐，需 L1 free 佐证）。输出 markdown 报告 + summary.json
  （写到 bundle 同级报告目录，bundle 只读）。**前提：目标训练必须启用
  torchtitan-npu 的 `--training.enable-cpu-offload`**——否则 collector
  不会产生任何 probe 数据（NO_PINNED_VMA），本 analyzer 无输入。
  当用户需要分析 CPU offload 内存数据、判读 offload 内存 node 分布、
  判断 nova 是否每步重建时触发；采集动作由 collector 负责。
license: MulanPSL-2.0
compatibility: 任意有 python3 >= 3.6 的环境（纯 stdlib，跨平台）；输入为 collector 的 bundle 目录（跨机可直接分析）；**要求 bundle 中的数据来自启用了 cpu offload 的训练**；只读 bundle（报告默认写到 bundle 同级报告目录）
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: cpu_offload
  version: "1.1.0"
---

# kptools-cpuoffload-analyzer — CPU offload 内存分析器

## 前置条件

**本 skill 分析的数据来自启用了 CPU offload 的 torchtitan-npu 训练。**

如果目标训练**没有**加 `--training.enable-cpu-offload`，进程不会有
`/dev/devmm_svm` 映射，collector 不会产生 probe 数据（NO_PINNED_VMA
rc=4），本 analyzer 无输入可分析。遇到空 bundle 错误时，先检查训练
启动命令。

CPU offload 的启用方法和背景见 `kptools-cpuoffload-collector` 的
SKILL.md"前置条件"节（MR !312，评审中；其 SKILL.md 含自足的启用
说明）。

## 工作流程

1. 确认 bundle 完整：至少有 1 个 `probe.*.json`（2 个以上才能做时间
   演化）——如果是空 bundle，说明**目标训练未启用 cpu offload**（见
   前置条件）
2. 分析：`python3 scripts/analyze.py /mnt/workspace/co`（bundle 目录）
3. stdout 是 markdown 报告（人类可读）；机器可读结论在报告目录的
   `summary.json`（默认 bundle 同级的 `bundle名_report/`，不写 bundle）

## 分析命令

```bash
python3 scripts/analyze.py /mnt/workspace/co
# 报告默认落 /mnt/workspace/co_report/（bundle 只读）
python3 scripts/analyze.py /mnt/workspace/co --out /mnt/workspace/report
```

## 三自检（不过不许出结论，退出码 3）

| # | 检查 | 判定 |
|---|------|------|
| 1 | Σ node_kb（含 unknown 桶）== total_present_kb | node 映射自洽（偏差 >5% → 失败；present>0 但 node 全零 → "无法验证"） |
| 2 | 快照时刻仍存活的 Σ alloc ≈ pagemap total（L1 存在时） | 按 result_ptr/arg_ptr 配对 alloc↔free 还原存活量——累计值会误伤"分配又回收"的正常序列（偏差 >10% → hook 装晚了或采样太粗；无可配对事件 → 无法验证） |
| 3 | 每段 Σ node_kb == 该段 present_kb | 采样放大没算错（同样含显式零值 → 无法验证） |

自检失败时报告**仍输出**（可见性优先），但退出码 3、报告标注"以下
结论不作数"，调用方按退出码分诊。自检 2 无 L1 数据时明确标"跳过"，
不显示"全部通过"。

## 判定能力

| 问题 | 判据 | 严重度 |
|------|------|--------|
| nova slab 每步重建 | 相邻快照**共同 rank** 增量 > 0.5 GiB | high（每步锁新页是浪费） |
| nova slab 驱逐（正常） | 共同 rank 骤降 > 0.5 GiB | info（候选解释：空 slab 驱逐 max_cached_bytes=0——用 L1 free 事件佐证） |
| rank 集合变化 | 相邻快照 rank/PID 集合差异 | info（探测缺口，非内存事件——单独披露不并入增量） |
| 内存 node 分裂 | top node 占比 < 60%（KB 口径） | medium（跨 node 远程访存嫌疑） |
| node 归属不可判定 | 主桶为 unknown（PFN 屏蔽） | info（降级，总量仍正确） |

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| bundle 目录 | collector 输出目录（位置参数） | 必填 |
| `--out <dir>` | 报告输出目录 | bundle 同级 `bundle名_report/` |

退出码：`0` 成功；`1` 参数错误（含 argparse 用法错误——不与 2 撞码）；
`2` bundle 为空（**大概率训练未启用 cpu offload**）；`3` 三自检失败
（报告仍输出，结论不作数）。

## 约束

- **只读 bundle**：不修改/不新增 bundle 内任何文件；报告默认写 bundle
  同级 `bundle名_report/`（`--out` 显式指定别处亦可），写入用
  临时文件 + 原子替换
- 三自检失败 → 退出码 3，报告仍输出（可见性优先），`summary.json`
  的 `self_checks.passed=false` + issues 明细
- L1 埋点存在时自动做存活配对交叉核对与池拆分/步数统计，不存在时
  自检 2 标"跳过"
- 解析缺口（probe/jsonl 坏行、尾部半行）进入 `parse_warnings` 披露，
  不静默吞
- driver 内部为什么选这个 node、die 级归属、bounce buffer 的毫秒级
  寿命——用户态不可见，报告口径声明中自报

### 和同类 skill 的分界

- 采集（找目标机、采样到落盘）：`kptools-cpuoffload-collector`
  （MR !312，评审中——未合入时本 skill 也可对符合 data-contract 的
  手工 bundle 分析）
- 线程绑核分析：`kptools-thread-affinity-analyzer`（MR !249，评审中）
- 训练 rank 的 numa_maps 内存快照：`kptools-train-mem-affinity`
  （MR !198，评审中）

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 2 | bundle 为空（无 probe.*.json）——**大概率是训练未启用 cpu offload**，先检查 `--training.enable-cpu-offload`；确认已跑过 collector |
| 退出码 3 | 自检未过——看 stderr 的 CHECK FAIL 与报告"数据缺口"节；自检 1/3 失败换 `--full` 重采，自检 2 失败查 L1 注入时机（PYTHONPATH 要在训练启动前生效） |
| 自检 1"无法验证" | present>0 但 node_kb 全零——检查 collector 版本（需 >= 1.1.0）与 zoneinfo 可读性 |
| parse_warnings 有"半行" | bundle 是采集进行中拷走的——等采集结束再分析，或忽略该行 |
| after_init 中 nova = 0 | 预期行为（还没发生）——继续采 step_1 |

## 参考资料

- [数据契约](references/data-contract.md) — collector 产物（1.1.0
  契约：node_kb 恒 KB / ptr 配对 / partial 披露）与 analyzer 输入/
  输出的完整 schema
