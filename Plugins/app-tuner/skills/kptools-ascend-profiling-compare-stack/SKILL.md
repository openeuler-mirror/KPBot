---
name: kptools-ascend-profiling-compare-stack
description: |
  只做两份有栈 NPU trace 的 Python 调用栈 diff：函数新增/消失、self 时间变化的双向 Top N、
  关键调用路径变化，并显式给出名称归一化与机械层折叠口径。输入必须是
  trace_view.json 含 cat=python_function（with_stack=true）；任一方无栈时明确输出「不可比」，
  不退化成 cpu_op 树硬比、不伪造 Δ。
  单份调用树分析走 `kptools-ascend-profiling-analysis`；算子维度（种类/次数/shape/分位）
  看 `kptools-ascend-profiling-compare-opstats`；通用两份 profiling 对比（步耗时/四分类/
  瓶颈迁移）看 `kptools-ascend-profiling-compare`；不确定用哪个对比能力时走
  `kptools-ascend-profiling-compare-hub`。
  注：compare / compare-opstats / compare-hub 与本 skill 为同批 PR 交付，合入前暂用
  kptools-ascend-profiling-analysis / -analysis-nostack 承接。
---

# 两份有栈 trace 的调用栈 diff（stack）

## 适用范围

- 同一负载优化前后、两个版本/配置的两份**有栈** trace：调用树里谁新增、谁消失、谁的 self 时间变了。
- 用 `--strict-line` 关闭行号归一化，用于确认行号漂移是否被合并。

不适用：

- 单份 trace 的调用树分析 / 热点定位 → `kptools-ascend-profiling-analysis`。
- 算子集合、shape、分位数 → `kptools-ascend-profiling-compare-opstats`（同批 PR 交付；合入前暂用 `kptools-ascend-profiling-analysis` 承接）。
- 设备 kernel、气泡、拷贝、stream、融合 → 对应专项 skill。
- 任一方无 `python_function`：本 skill 只报「不可比」，不做替代推断。

## 输入

- 两份采集目录、`*_ascend_pt`、`ASCEND_PROFILER_OUTPUT` 或裸 `trace_view.json`。
- 两侧都必须开启 `with_stack=true`（trace 中要有 `cat=python_function` 事件）。
- 依赖上游索引脚本（subprocess 调用，不 import）：
  `kptools-ascend-profiling-analysis/scripts/build_index.py`，兼容
  `skills/<name>/` 与 `skills/<domain>/<name>/` 两种布局；找不到时直接输出「不可比」。
- 索引缓存 key = trace 绝对路径 + 文件大小 + mtime 的 sha1，落在
  `~/.cache/kpbot_profiling_stack/`（可用 `KPBOT_STACK_CACHE_DIR` 覆盖）。

## 工作流

```bash
python3 scripts/stack_diff.py <base> <cand> \
  --out-html stack_diff.html --json stack_diff.json \
  [--strict-line] [--min-delta-ms 0.05] [--top 10]
```

1. 确认两份采集来自同一负载（模型/版本/rank 由使用者确认；本 skill 不做 workload 一致性推断）。
2. 读 HTML：新增/消失函数、self 变多/变少双向 Top、关键路径示例、归一化与折叠说明。
3. 需要机器聚合时读 `--json`（A.1 统一信封，指标前缀 `stack_`）。

## 输出

| name | unit | 含义 |
|---|---|---|
| `stack_func_count` | count | 归一化去重后的函数数（base / cand） |
| `stack_total_self_ms` | ms | 所有函数 self 时间之和 |
| `stack_top_self_ms` | ms | \|self Δ\| 最大函数的 self 时间（函数名在 `evidence`） |
| `stack_new_funcs` | count | 只在候选侧出现（`base=0, cand=N, delta=−N`） |
| `stack_gone_funcs` | count | 只在基线侧出现（同上） |
| `stack_changed_funcs` | count | \|self Δ\| > `--min-delta-ms` 的共有函数数 |

- `per_step`：每步的 `stack_total_self_ms`（按 step 序号配对）。
- 计数型指标（new/gone/changed）固定 `base=0`、`verdict=info`，是集合差异不是量的 Δ。
- `Δ = base − cand`，正值为候选更省；时间类指标 lower is better。

## 降级路径（始终产出 HTML + JSON）

1. 任一方无 `python_function` → 「不可比」报告：写原因、`confidence=low`、退出码 0，不伪造 Δ。
2. 找不到 `build_index.py`（两种布局都没有）→ 该侧 `no_index`，原因写进 `degraded`，退出码 0。
3. 部分函数名带绝对路径/行号 → 归一化合并；归一化失败归 `unresolved` 并在 `notes` 给数量。
4. 索引构建失败/超时（含上游 `build_index.py` 在无设备事件的纯 host trace 上崩溃）→ 该侧 `failed`，只给另一侧画像，不做 Δ，退出码 0。
5. 输入不可读 → HTML 错误页 + JSON，退出码 3。

## 口径与限制

- **树**：索引 `events` 表（`cat='python_function'`）按 `(pid,tid)` 分组、按 `ts_ns` 排序，
  用区间包含建立父子关系（父 = 最近一个 `ts ≤ 子 ts` 且 `end ≥ 子 end` 的事件）。
- **self（帧独占经过时间）**：`帧区间 − 直接子帧区间`，按各 step 窗口的交集分摊到逐步统计；
  同一归一化函数名跨实例求和。该值跨线程累计、包含线程阻塞/被调度出去的等待，**不是 CPU 时间**；
  判断 CPU 占用需线程 CPU 时钟或调度证据。**total** = 节点自身区间时长。
- **折叠**：复制上游 `calltree.py` 的 `COLLAPSE_SUBSTR`（17 条机械层）；被折叠帧不参与聚合，
  其子节点上提，折叠帧自身时间归父。
- **归一化**：默认去掉 `(file:line)` 后缀、把 `file.py(N)` 归一为 `file.py(*)`、剥离目录
  前缀（含相对目录）；`--strict-line` 保留原样（用于确认行号漂移）。归一化后仍无法识别的记 `unresolved`。
- **层级边界**：只统计 host 侧 python frame，不与 device kernel 时间相加；
  跨 rank 的绝对时间不可叠加。
- **上游依赖边界**：`build_index.py` 需要 trace 含设备事件；纯 host（无 `AI_CORE` 等）trace
  会让其构建失败，本 skill 记为 `failed` 并判不可比，不退化成 cpu_op 树。
- **置信度**：两侧均有 python_function 且无降级 = `medium`（本 skill 只消费 trace，
  按 A.1 规则不可能为 high）；任一降级/不可比 = `low`。

## 参考资料

- [references/stack-diff-rules.md](references/stack-diff-rules.md) — 建树、self/total、折叠、名称归一化与行号漂移容忍。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_stack -v
```

## 真实数据口径

- `self` 总和是**跨线程累计的帧独占经过时间之和**（含阻塞/等待，**不是 CPU 时间**），多线程场景下可以超过墙钟时间（实测 2137ms self / 810ms 墙钟），报告需如此解读；判断 CPU 占用需线程 CPU 时钟或调度证据。
- 真实 trace 的 `python_function` 事件可达 20 万级；`build_index.py` 两级布局查找 + 索引缓存可避免重复解析。
- 任一方无 `python_function` → 输出「不可比」；时间单位 µs。
