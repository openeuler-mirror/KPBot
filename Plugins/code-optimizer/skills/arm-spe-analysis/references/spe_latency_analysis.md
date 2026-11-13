# ARM SPE 深度时延分析指南

> 本参考文档对应 `arm-spe-analysis` skill 的**深度时延分析**阶段（第 3.5 步/进阶路径）：
> `spe-csv.sh`（标准 CSV 解析，支持外部开源 spe-parser）→ `spe-latency-report.py`（深度分析与报告）。

## 目录
1. [标准 CSV Schema](#1-标准-csv-schema)
2. [深度分析流程](#2-深度分析流程)
3. [指标定义与门禁阈值](#3-指标定义与门禁阈值)
4. [时延字段的获取与解读](#4-时延字段的获取与解读)
5. [高时延根因归因方法论](#5-高时延根因归因方法论)
6. [接入外部开源 spe-parser](#6-接入外部开源-spe-parser)
7. [与既有脚本的关系](#7-与既有脚本的关系)
8. [局限性与注意事项](#8-局限性与注意事项)

---

## 1. 标准 CSV Schema

`spe-csv.sh` / `spe-to-csv.py` 产出的 CSV 为 13 列统一 schema。任何开源 spe-parser
输出符合该 schema 即可被下游直接消费：

| 列 | 含义 | 来源 | 可缺省 |
|----|------|------|--------|
| `comm` | 进程/线程名 | perf script | 否 |
| `pid` | 进程 PID | perf script | 否 |
| `tid` | 线程 TID | `-F +tid` | 是 |
| `cpu` | 采样 CPU | perf script | 否 |
| `time` | 采样时间戳（秒） | perf script | 否 |
| `period` | 采样周期计数值 | perf script | 否 |
| `event` | SPE 事件类型（l1d-miss/llc-miss/tlb-access/tlb-miss/branch-miss/remote-access…） | SPE Events Packet | 否 |
| `ip` | 指令地址 PC | SPE Address Packet | 否 |
| `sym` | 符号（函数+偏移，如 `func_hot+0x10`） | perf 符号化 | 是 |
| `dso` | 动态库/可执行文件 | perf | 是 |
| `weight` | 指令延迟（周期数） | SPE Latency Packet → perf `weight` | 是 |
| `data_src` | 数据来源层级（字符串或数值 0-7） | SPE Data Source Packet → perf `data_src` | 是 |
| `addr` | 数据地址（访存指令有效） | SPE Address Packet → perf `addr` | 是 |

> 时延字段缺省时，报告会明确标注"无 weight 字段"，仅提供频率型分析（采样数热点、
> miss 率），并提示使用完整字段重新解析。

## 2. 深度分析流程

```text
1. 采集      → spe-collect.sh [-f load|store|branch] -o spe.data
2. 解析 CSV  → spe-csv.sh -o samples.csv spe.data          # 或接入外部 spe-parser
3. 校验      → spe-csv.sh --validate samples.csv
4. 深度分析  → spe-latency-report.py samples.csv -o report.md [--json report.json]
5. 前后对比  → spe-latency-report.py samples.csv --baseline before.csv -o diff.md
```

报告章节：

| 章节 | 内容 |
|------|------|
| 0. 执行摘要 | 自动挑选 SEVERE/MODERATE 级别的瓶颈信号 |
| 1. 指标门禁判定 | l1_miss/llc_miss/branch_mispred/tlb_miss/remote_ratio/p99_latency/tail_ratio 分级表 |
| 2. 指令时延分布 | 均值/P50/P90/P99/P99.9/最大值 + 直方图 |
| 3. 缓存命中层级 | data_src 分布（L1/L2/L3/Peer/Remote/DRAM）+ 各层级平均时延 |
| 4. 指令追逐 | 逐指令(PC)与逐函数双热点：采样数 vs 加权时延(Σlatency)；P99 尾部指令表 |
| 5. 高时延根因分析 | 尾部样本单因子归因 + 组合根因共现表 |
| 6. 补充指标 | 事件类型分布、指令类型(load/store/branch)、CPU 分布 |
| 7. 前后对比 | 函数级指标 Δ 与 IMPROVED/REGRESSED/NO CHANGE 判定（`--baseline`） |

## 3. 指标定义与门禁阈值

沿用 `spe-hotspot.sh` 的 SEVERE/MODERATE/MILD/OK 判定风格（门禁式检查项）：

| 指标 | 定义 | SEVERE | MODERATE | MILD | OK |
|------|------|--------|----------|------|----|
| `l1_miss` | l1d-miss 样本 / 总样本 | >30% | >10% | >5% | ≤5% |
| `llc_miss` | llc-miss 样本 / 总样本 | >30% | >10% | >5% | ≤5% |
| `branch_mispred` | branch-miss 样本 / 总样本 | >20% | >5% | >1% | ≤1% |
| `tlb_miss` | tlb-miss 样本 / 总样本 | >10% | >5% | >2% | ≤2% |
| `remote_ratio` | remote-access 样本 / 总样本 | >20% | >10% | >5% | ≤5% |
| `p99_latency` | P99 时延（cycles） | >1000 | >300 | >100 | ≤100 |
| `tail_ratio` | 时延 ≥1000 cycles 样本占比 | >10% | >5% | >1% | ≤1% |

> 前 3 项阈值与 `spe-hotspot.sh` 保持一致；tlb/remote/p99/tail 为深度阶段新增的
> 参考阈值，可按场景调整（`GRADES` 常量在 `spe-latency-report.py` 顶部）。

**加权时延（Σlatency）**：以 `样本数 × 平均时延`（即该 PC 上采样样本的时延总和）
度量"指令对总时延的贡献"。与纯采样数排名不同，它能暴露"采样不多但每条都很慢"
的严重指令——这是深度阶段相对 `spe-parse.sh` 的关键增量。

## 4. 时延字段的获取与解读

- `weight` 来自 SPE Latency Packet，perf 通过 `-F +weight` 暴露，表示该指令的
  执行延迟（周期数）。需要内核 ≥5.10 且 `CONFIG_ARM_SPE_PMU` 使能、perf 与内核
  版本匹配。
- `data_src` 数值编码遵循 SPE Data Source Packet：`1=L1`、`2=L2`、`3=L3/LLC`、
  `4=Peer Cache`、`5=Remote Cache`、`6=本地 DRAM`、`7=远端 DRAM`。`spe-latency-report.py`
  同时支持 perf 输出的人读字符串（`L1_or_0x0`、`L2_or_0x0`、`RAM` 等）。
- 时延是**统计性样本**：SPE 随机采样，非逐条指令全记录。结论应基于分布与占比，
  单个样本的绝对值不可过度解读。

## 5. 高时延根因归因方法论

1. 取**尾部样本**（时延 ≥ 阈值，默认 500 cycles，可用 `--min-lat` 调整）。
2. **单因子归因**：统计尾部样本中包含"DRAM / REMOTE / PEER / L3 / L2 / L1 /
   tlb_miss / branch_mispred"各因子的占比（因子可重叠，占比合计可 >100%）。
3. **组合根因**：按标签共现（如 `remote_or_dram`、`remote_or_dram+tlb_miss`）
   统计组合，用于区分"单点根因"与"多因子叠加"。
4. 结合第 4 节的 P99 尾部指令表，把根因因子落到具体 PC/函数。

典型结论范式：
- 尾部样本集中在 REMOTE/DRAM 且伴随 tlb_miss → 访存远距离 + 页表遍历叠加，优先查
  NUMA 亲和、大页、数据布局。
- 尾部集中在特定 PC 的 llc-miss → 工作集超容或 false sharing，查数据分块/对齐。
- 尾部集中在 branch-mispred → 分支模式不可预测，查分支消除/热冷分离。

## 6. 接入外部开源 spe-parser

`spe-csv.sh --parser "<CMD>"` 允许接入任意开源 spe-parser（如第三方 SPE 解码器）：

```bash
bash spe-csv.sh --parser "python3 /opt/spe-parser/parse.py" spe.data > samples.csv
bash spe-csv.sh --parser "spe-parser --out csv" spe.data > samples.csv
```

约定：
- CMD 从 stdin 读取 `perf script` 输出，向 stdout 写**标准 CSV**（13 列 schema）。
- 接入后 `spe-csv.sh` 会自动校验输出 schema，不匹配即报错，避免脏数据流入报告。
- 外部解析器应保留 `perf script` 的 `weight`/`data_src`/`addr` 字段映射；
  若外部工具无法输出这些字段，深度时延章节会自动降级为频率型分析。

## 7. 与既有脚本的关系

| 脚本 | 分析模型 | 定位 |
|------|----------|------|
| `spe-parse.sh` | 事件类型（event-type）百分比 | 快速汇总 + 按指令/函数聚合 |
| `spe-hotspot.sh` | miss/分支预测率 | 热点与瓶颈分级定位 |
| `spe-compare.sh` | 事件类型百分比 diff | 优化前后对比 |
| `spe-csv.sh` | perf script → 标准 CSV | **深度分析的数据入口**（含外部 spe-parser 接入） |
| `spe-latency-report.py` | 时延分布 + 命中层级 + 加权热点 + 根因 | **深度分析与报告** |

深度阶段与既有阶段的关系：先用 `spe-parse.sh --summary` 快速概览 → 若有
时延/缓存层级/长尾分析需求，走 `spe-csv.sh` + `spe-latency-report.py` →
确认瓶颈后回到 `spe-hotspot.sh`/`spe-compare.sh` 做优化前后闭环。

## 8. 局限性与注意事项

- **无 weight/data_src 的降级**：`perf script -F weight,data_src` 失败（perf/内核
  版本旧）时，`spe-csv.sh` 自动回退基础字段并告警；报告对应章节会标注"无此字段"。
  此时时延/层级结论不可下。
- **事件覆盖**：只采集 load 时无法分析 store；只采集 branch 时无法分析访存层级。
  根因归因需在对应 filter 的采集上做。
- **采样统计性**：单样本不可过度解读；建议 `--top` 结合分布（P50/P90/P99）看，
  而非只看最大值。
- **数据源字符串依赖 perf 版本**：不同 perf 的 `data_src` 人读格式可能不同，
  数值编码 (0-7) 为最稳定的输入。
- **CPU 归一化**：跨 CPU 时延分布可反映负载不均（见报告 6.3 节），但 SPE 采样是
  概率性的，核间样本数差异大时需谨慎。
