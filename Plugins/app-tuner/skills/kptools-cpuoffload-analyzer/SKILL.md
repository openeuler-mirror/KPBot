---
name: kptools-cpuoffload-analyzer
description: >
  CPU offload 内存分析器：读取 kptools-cpuoffload-collector 的 bundle
  目录（probe.<tag>.json 快照序列 + 可选 L1 alloc/marks + L2 stats），
  执行三条自检（node 映射自洽 / L1 交叉核对 / 比例放大一致），自动
  判定：总量时间演化、nova slab 是否每步重建、内存 node 亲和、pinned
  骤降（空 slab 驱逐）。输出 markdown 报告 + summary.json。**前提：
  目标训练必须启用 torchtitan-npu 的 `--training.enable-cpu-offload`**
  ——否则 collector 不会产生任何 probe 数据（NO_PINNED_VMA），本
  analyzer 无输入。当用户需要分析 CPU offload 内存数据、判读 offload
  内存 node 分布、判断 nova 是否每步重建时触发。
license: MulanPSL-2.0
compatibility: 任意有 python3 >= 3.6 的环境（纯 stdlib，跨平台）；输入为 collector 的 bundle 目录（跨机可直接分析）；**要求 bundle 中的数据来自启用了 cpu offload 的训练**；只读 bundle，不修改采样数据
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

CPU offload 的启用方法和背景见
`kptools-cpuoffload-collector` 的 SKILL.md"前置条件"节。

## 工作流程

1. 确认 bundle 完整：至少有 1 个 `probe.*.json`（2 个以上才能做时间演化）
   ——如果是空 bundle，说明**目标训练未启用 cpu offload**（见前置条件）
2. 分析：`python3 scripts/analyze.py <bundle>`
3. stdout 是 markdown 报告（人类可读）；机器可读结论在 `summary.json`

## 分析命令

```bash
python3 scripts/analyze.py /mnt/workspace/co
python3 scripts/analyze.py /mnt/workspace/co --out /mnt/workspace/report
```

## 三自检（不过不许出结论）

| # | 检查 | 判定 |
|---|------|------|
| 1 | Σ node_kb + unknown == total_present_kb | node 映射自洽（偏差 >5% → 自检失败） |
| 2 | Σ alloc.size ≈ pagemap total（L1 存在时） | 两个视图说同一件事（偏差 >10% → hook 装晚了或采样太粗） |
| 3 | 每段 Σ node_kb == 该段 present_kb | 采样放大没算错 |

## 判定能力

| 问题 | 判据 | 严重度 |
|------|------|--------|
| nova slab 每步重建 | after_init → step_1 增量 > 0.5 GiB | high（每步锁新页是浪费） |
| nova slab 驱逐（正常） | step 序列骤降 > 0.5 GiB | info（max_cached_bytes=0 的预期行为） |
| 内存 node 分裂 | top node 占比 < 60% | medium（跨 node 远程访存嫌疑） |
| node 归属不可判定 | PFN 权限缺失 → 全 unknown | info（降级，总量仍正确） |

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `<bundle>` | collector 输出目录 | 必填 |
| `--out <dir>` | 报告输出目录 | bundle 目录 |

## 约束

- 只读 bundle，不修改采样数据
- 三自检失败时报告标注"以下结论不作数"但**仍输出**（退出码仍为 0，
  让报告可见——自检结果在 `summary.json` 的 `self_checks.passed` 字段）
- L1 埋点数据（alloc/marks jsonl）存在时自动做交叉核对，不存在时跳过
- driver 内部为什么选这个 node、die 级归属、bounce buffer 的毫秒级
  寿命——用户态不可见，报告口径声明中自报

### 和同类 skill 的分界

- 采集（找目标机、采样到落盘）由 `kptools-cpuoffload-collector` 覆盖
- 线程绑核分析由 `kptools-thread-affinity-analyzer` 覆盖
- 训练 rank 的 numa_maps 内存快照由 `kptools-train-mem-affinity` 覆盖

## 错误处理

| 现象 | 原因与处理 |
|------|-----------|
| 退出码 2 | bundle 为空（无 probe.*.json）——**大概率是训练未启用 cpu offload**，先检查 `--training.enable-cpu-offload`；确认已跑过 collector |
| 自检 1 失败 | pagemap 采样太粗或 collector 的 zoneinfo 解析有 bug——换 --full 重新采集 |
| 自检 2 失败 | hook 装晚了（T5 之前没生效）——L1 注入要加到 PYTHONPATH 后再启动训练 |
| after_init 中 nova = 0 | 预期行为（还没发生）——继续采 step_1 |

## 参考资料

- [数据契约](references/data-contract.md) — collector 产物与 analyzer 输入/输出的完整 schema
