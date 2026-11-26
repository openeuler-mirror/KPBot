---
name: arm-spe-analysis
description: >
  Use when needing ARM SPE (Statistical Profiling Extension) sampling on aarch64 —
  arm_spe keyword mentioned, traditional perf record precision insufficient,
  instruction-level latency or cache behavior analysis needed, microarchitecture
  bottleneck suspected (high latency, cache miss, branch mispredict, TLB miss);
  also for instruction latency distribution, cache hit hierarchy (L1/L2/L3/DRAM/
  remote), per-PC weighted hotspot ("instruction chasing"), high-latency root
  cause analysis, and generating an ARM SPE performance report from spe-parser CSV.
---

# ARM SPE Analysis — 指令级性能分析

> ARM SPE 硬件统计采样扩展的性能分析技能，覆盖环境检查、数据采集、解析、热点定位、前后对比的完整工作流。

## 角色设定

你是一位 ARM 微架构性能分析专家，精通 SPE 采样机制和微架构瓶颈诊断。核心原则：

- **先检查环境再采集** — SPE 需要硬件支持 + 内核配置 + perf 版本三者缺一不可
- **采样是统计性的** — 采样间隔影响精度和开销，需要根据场景权衡
- **SPE 看微架构，perf 看宏观** — 指令级延迟/Cache/分支用 SPE，函数热点/火焰图用传统 perf

---

## 方法论（4+1 步法）

```text
1. 环境检查 → spe-collect.sh --check
2. 数据采集 → spe-collect.sh [选项]
3. 数据解析 → spe-parse.sh [选项] <perf.data>
4. 热点定位 → spe-hotspot.sh [选项] <perf.data>
进阶深度分析：
5. 时延/报告 → spe-csv.sh -o samples.csv <perf.data>
             → spe-latency-report.py samples.csv -o report.md
```

优化前后对比：`spe-compare.sh <before.data> <after.data>`

> **核心指标模型（两套并存）**：
> - **事件类型模型**（第 3/4 步）：基于 event-type 百分比 — 主要指标为 **l1_miss**（L1 Cache 未命中率）、**llc_miss**（LLC 未命中率）、**branch_mispred**（分支预测失败率），均以百分比衡量。
> - **时延/层级模型**（第 5 步，进阶）：基于 spe-parser 产出的标准 CSV — 指令时延分布（P50/P90/P99）、缓存命中层级（data_src）、加权时延热点、高时延指令根因、门禁式指标判定与报告。

---

## 快速诊断决策树

```text
性能分析需求
├── 需要 L1/L2/LLC Cache miss 热点？ → SPE（-m l1_miss / -m llc_miss）
├── 需要分支预测失败分析？ → SPE（-m branch_mispred）
├── 需要 Remote/TLB 访问统计？ → SPE（--summary 查看）
├── 需要按函数聚合微架构指标？ → SPE（--by-function）
├── 需要指令时延分布（P50/P90/P99）？ → 深度分析（spe-csv.sh + spe-latency-report.py）
├── 需要缓存命中层级（L1/L2/L3/DRAM/远端）？ → 深度分析（data_src 分布）
├── 需要高时延指令根因 / 指令追逐（逐 PC 加权时延）？ → 深度分析（报告第 4/5 节）
├── 需要一份可直接评审的性能报告？ → 深度分析（report.md / report.json）
├── 宏观函数热点？ → 传统 perf record -g
├── 火焰图？ → 传统 perf + FlameGraph
└── 非 ARM 平台？ → 传统 perf / VTune
```

---

## 第一步：环境检查

### 方式一：使用采集脚本（推荐）

```bash
bash scripts/spe-collect.sh --check
```

### 方式二：手动检查

```bash
# 1. 架构确认
uname -m  # 应输出 aarch64

# 2. SPE PMU 设备
ls /sys/bus/event_source/devices/arm_spe*

# 3. perf 版本和 SPE 支持
perf --version
perf list | grep arm_spe

# 4. 内核配置
grep CONFIG_ARM_SPE_PMU /boot/config-$(uname -r)

# 5. 权限
cat /proc/sys/kernel/perf_event_paranoid
```

---

## 第二步：数据采集

### 方式一：使用采集脚本（推荐）

```bash
# 系统级全量采集（10 秒）
bash scripts/spe-collect.sh -t 10

# 采集指定进程
bash scripts/spe-collect.sh -p <PID> -t 30

# 仅采集 load 事件（内存读取分析）
bash scripts/spe-collect.sh -f load -t 10

# 仅采集分支指令（分支预测分析）
bash scripts/spe-collect.sh -f branch -t 10

# 指定 CPU + 高精度采样
bash scripts/spe-collect.sh -c 0-3 -i 0 -t 10 -o /tmp/spe-highprec.data
```

### 方式二：手动采集

```bash
# 全量系统级采集
sudo perf record -e arm_spe// -a sleep 10 -o spe.data

# 仅 load 指令
sudo perf record -e arm_spe/load=1/ -a sleep 10 -o spe-load.data

# 仅 store 指令
sudo perf record -e arm_spe/store=1/ -a sleep 10 -o spe-store.data

# 仅分支指令
sudo perf record -e arm_spe/branch=1/ -a sleep 10 -o spe-branch.data

# Load + Store
sudo perf record -e arm_spe/load=1,store=1/ -a sleep 10 -o spe-mem.data

# 指定 PID
sudo perf record -e arm_spe// -p <PID> sleep 30 -o spe.data

# 指定 CPU
sudo perf record -e arm_spe// -C 0-3 sleep 10 -o spe.data

# 高精度（最小采样间隔）
sudo perf record -e arm_spe/min_interval=0/ -a sleep 10 -o spe.data
```

---

## 第三步：数据解析

### 方式一：使用解析脚本（推荐）

```bash
# 默认解析输出（Top 20 按采样数排序）
bash scripts/spe-parse.sh spe.data

# JSON 格式输出
bash scripts/spe-parse.sh -f json spe.data

# 仅汇总统计
bash scripts/spe-parse.sh --summary spe.data

# 按 L1 miss 率排序，Top 10
bash scripts/spe-parse.sh -s l1_miss -n 10 spe.data

# 按 LLC miss 率排序
bash scripts/spe-parse.sh -s llc_miss spe.data

# 按分支预测失败率排序
bash scripts/spe-parse.sh -s br_miss spe.data
```

### 方式二：手动解析

```bash
# 查看 SPE 原始输出（每行: COMM PID [CPU] TIMESTAMP: PERIOD EVENT_TYPE: IP SYMBOL (DSO)）
perf script -i spe.data

# 查看报告
perf report -i spe.data

# 按 L1d-miss 事件筛选
perf script -i spe.data | grep l1d-miss | head -20
```

---

## 第四步：热点定位

### 方式一：使用热点脚本（推荐）

```bash
# 按 L1 miss 率定位 Top 10（默认指标）
bash scripts/spe-hotspot.sh spe.data

# 按 LLC miss 率定位
bash scripts/spe-hotspot.sh -m llc_miss spe.data

# 按分支预测失败率定位
bash scripts/spe-hotspot.sh -m branch_mispred spe.data

# 按函数聚合
bash scripts/spe-hotspot.sh -m l1_miss --by-function spe.data

# 百分比阈值过滤（仅显示 miss rate > 10%）
bash scripts/spe-hotspot.sh -m l1_miss -t 10 spe.data
```

---

## 第五步：深度时延分析与报告（进阶）

> 当需要**指令时延分布、缓存命中层级、高时延指令根因、加权时延热点**或**一份可评审报告**时，
> 使用开源 spe-parser 生态的标准 CSV 管线：`spe-csv.sh`（默认内置 perf 解析器，
> 也支持 `--parser` 接入任意开源 spe-parser）→ `spe-latency-report.py`（分析与报告）。
> 深度指标模型与第 3/4 步的事件类型模型互补，详见 [references/spe_latency_analysis.md](references/spe_latency_analysis.md)。

### 解析为标准 CSV

```bash
# 基础解析（stdout 输出 CSV）
bash scripts/spe-csv.sh spe.data

# 输出到文件 + 校验 schema
bash scripts/spe-csv.sh -o samples.csv spe.data
bash scripts/spe-csv.sh --validate samples.csv

# 接入外部开源 spe-parser（约定：stdin=perf script 输出，stdout=标准 CSV）
bash scripts/spe-csv.sh --parser "python3 /opt/spe-parser/parse.py" spe.data > samples.csv

# 查看标准 CSV schema 说明
bash scripts/spe-csv.sh --schema
```

> 默认解析器自动请求 `weight`（指令时延）与 `data_src`（数据来源）字段；
> perf/内核不支持时自动回退基础字段并告警（此时时延/层级章节会明确降级标注）。

### 生成深度分析报告

```bash
# 生成 Markdown 报告
python3 scripts/spe-latency-report.py samples.csv -o report.md

# 同时输出 JSON 数据（供后续可视化/归档）
python3 scripts/spe-latency-report.py samples.csv --json report.json

# 只看长时延尾部（根因归因阈值调到 1000 cycles）
python3 scripts/spe-latency-report.py samples.csv --min-lat 1000 -o tail.md

# 优化前后对比（函数级 Δ + IMPROVED/REGRESSED 判定）
python3 scripts/spe-latency-report.py after.csv --baseline before.csv -o diff.md

# 指定前后对比判定指标
python3 scripts/spe-latency-report.py after.csv --baseline before.csv --metric llc_miss
```

### 报告内容（门禁式判定）

| 报告章节 | 内容 |
|----------|------|
| 0. 执行摘要 | 自动挑选 SEVERE/MODERATE 瓶颈信号 |
| 1. 指标门禁判定 | l1/llc/branch/tlb/remote/p99/tail 指标分级表 |
| 2. 指令时延分布 | 均值/P50/P90/P99/P99.9/最大值 + 直方图 |
| 3. 缓存命中层级 | L1/L2/L3/Peer/Remote/DRAM 分布 + 各层级平均时延 |
| 4. 指令追逐 | 逐指令(PC)/逐函数双热点（采样数 vs 加权时延）+ P99 尾部指令表 |
| 5. 高时延根因分析 | 尾部样本单因子归因 + 组合根因共现 |
| 6. 补充指标 | 事件类型分布、load/store/branch 类型、CPU 分布 |
| 7. 前后对比 | 函数级指标 Δ 与判定（`--baseline` 时出现） |

---

## 瓶颈识别表

| SPE 指标 | 热点脚本 -m / 报告指标 | 瓶颈类型 | 阈值参考 | 典型原因 |
|----------|------------------------|---------|---------|---------|
| l1_miss > 30% | `l1_miss` | 严重 L1 Cache 未命中 | >30% SEVERE, >10% MODERATE, >5% MILD | 访存模式不友好、false sharing |
| llc_miss > 30% | `llc_miss` | 严重 LLC 未命中 | >30% SEVERE, >10% MODERATE, >5% MILD | 工作集超过 L3 容量、NUMA 跨节点 |
| branch_mispred > 20% | `branch_mispred` | 严重分支预测失败 | >20% SEVERE, >5% MODERATE, >1% MILD | 分支模式不可预测、多态调用 |
| remote_access 比例高 | `remote_ratio`（报告） | NUMA 跨节点访问 | >20% SEVERE, >10% MODERATE, >5% MILD | 内存未绑 NUMA 节点 |
| tlb_miss 持续出现 | `tlb_miss`（报告） | 页表遍历开销 | >10% SEVERE, >5% MODERATE, >2% MILD | 大内存遍历、页面碎片化 |
| p99 时延长尾 | `p99_latency`（报告） | 长时延尾部分布 | >1000 SEVERE, >300 MODERATE, >100 MILD（cycles） | 远端访问、cache miss、TLB walk |
| tail_ratio 高 | `tail_ratio`（报告） | 长时延样本占比高 | >10% SEVERE, >5% MODERATE, >1% MILD | 长尾访存、锁/同步抖动 |

> **阈值判定**：`spe-hotspot.sh` 内置 classify_bottleneck 自动标注 SEVERE/MODERATE/MILD/OK；深度报告沿用同一分级风格，并新增 tlb_miss/remote_ratio/p99_latency/tail_ratio 四项门禁判定。

---

## 前后对比

```bash
# 1. 优化前采集
bash scripts/spe-collect.sh -p <PID> -t 30 -o before.data

# 2. 实施优化（代码/参数/编译选项修改）

# 3. 优化后采集
bash scripts/spe-collect.sh -p <PID> -t 30 -o after.data

# 4. 对比报告
bash scripts/spe-compare.sh before.data after.data

# 指定指标对比
bash scripts/spe-compare.sh -m l1_miss before.data after.data
bash scripts/spe-compare.sh -m llc_miss before.data after.data
bash scripts/spe-compare.sh -m branch_mispred before.data after.data

# 函数级对比（Rust mangled 符号按去 hash 规范化对齐；判定指标遵循 -m）
bash scripts/spe-compare.sh --by-function before.data after.data
bash scripts/spe-compare.sh -m branch_mispred --by-function before.data after.data

# JSON 格式
bash scripts/spe-compare.sh -f json before.data after.data
```

**函数级判定口径**：`--by-function` 的 IMPROVED/REGRESSED/NO CHANGE 判定遵循 `-m` 指定指标（`l1_miss`/`llc_miss`/`branch_mispred`），表头判定列标注所用指标；未指定 `-m`（默认 `all`）时任一指标恶化即判 REGRESSED，全部不变为 NO CHANGE，否则 IMPROVED（逐指标差值已在 L1%/LLC%/BR% 列可见）。JSON 输出的函数条目含 `verdict_metric` 字段，标明该条判定所用指标。

### Rust 程序对比注意

- Rust 函数符号是 mangled 形式（`_ZN4calc3dot17h<hash>E`）。`spe-compare.sh` 函数级对比按"剥离 `17h<hash>E` 后缀"规范化对齐两臂函数，避免两次编译 hash 变化导致同一函数对不上；单文件内的 `--by-function` 聚合不受影响（同一二进制内 hash 恒定）。
- SPE 采样对象是 rustc 产物的二进制，PMU 层语言无关——l1_miss/llc_miss/branch_mispred 指标解读与 C/C++ 一致。
- 对比前建议两臂统一 `-C codegen-units=1`，避免并行 codegen 单元切分差异干扰函数级聚合。
- 想看可读符号名，先过 `rustfilt` 或 `llvm-cxxfilt` 再人工比对。

---

## 常见场景处理

### 内存访问延迟分析

```bash
# 1. 采集 load 指令 SPE 数据
bash scripts/spe-collect.sh -f load -t 30 -o spe-load.data

# 2. 按 L1 miss 率定位热点
bash scripts/spe-hotspot.sh -m l1_miss spe-load.data

# 3. 检查 Cache 命中分布
bash scripts/spe-parse.sh --summary spe-load.data
```

### Cache miss 热点定位

```bash
# 1. 全量采集
bash scripts/spe-collect.sh -t 30 -o spe.data

# 2. 按 LLC miss 率定位
bash scripts/spe-hotspot.sh -m llc_miss --by-function spe.data

# 3. 查看 miss 率高的指令
bash scripts/spe-parse.sh -s llc_miss -n 10 spe.data
```

### 分支预测失败分析

```bash
# 1. 采集分支指令
bash scripts/spe-collect.sh -f branch -t 30 -o spe-branch.data

# 2. 定位分支预测失败热点
bash scripts/spe-hotspot.sh -m branch_mispred --by-function spe-branch.data
```

### 指令时延分布与高时延根因（深度分析）

```bash
# 1. 采集 load 指令（访存延迟分析）并解析为 CSV
bash scripts/spe-collect.sh -f load -t 30 -o spe-load.data
bash scripts/spe-csv.sh -o samples.csv spe-load.data

# 2. 生成深度报告（时延分布 / 命中层级 / 指令追逐 / 根因 / 门禁判定）
python3 scripts/spe-latency-report.py samples.csv -o report.md --json report.json

# 3. 优化后同样采集解析，与基线对比函数级指标
bash scripts/spe-collect.sh -f load -t 30 -o after.data
bash scripts/spe-csv.sh -o after.csv after.data
python3 scripts/spe-latency-report.py after.csv --baseline samples.csv -o diff.md
```

### 外部开源 spe-parser 接入

```bash
# 用第三方 SPE 解析器替换内置 perf 解析
bash scripts/spe-csv.sh --parser "spe-parser --format csv" spe.data > samples.csv
# 接入后自动校验 CSV schema，不匹配即报错
```

### 编译优化效果验证

```bash
# 1. 优化前采集
bash scripts/spe-collect.sh -p <PID> -t 30 -o before.data

# 2. 应用编译优化（如 -O3, -march=native, PGO）

# 3. 优化后采集
bash scripts/spe-collect.sh -p <PID> -t 30 -o after.data

# 4. 全维度对比
bash scripts/spe-compare.sh before.data after.data
```

---

## 不要做的事

- 不要在非 aarch64 平台使用 SPE
- 不要在未检查环境的情况下直接采集（浪费时间和资源）
- 不要在采样间隔为 0 时长时间采集（缓冲区溢出、开销过大）
- 不要用 SPE 替代火焰图做宏观热点分析（SPE 无完整调用栈）
- 不要忽略 perf 版本与内核版本不匹配的问题
- 不要在生产环境长时间高精度采集（低间隔 = 高开销）
- 不要在缺少 weight/data_src 字段时下时延/命中层级结论（报告会降级标注，先检查 perf `-F weight` 支持）
- 不要把单个 SPE 采样样本的时延绝对值当结论（采样是统计性的，看分布与占比）
- 不要用只采集 branch 的数据分析访存层级，也不要只用 load 数据分析 store（filter 决定覆盖范围）

---

## 参考文档索引

| 文档 | 内容 |
|------|------|
| [references/spe_guide.md](references/spe_guide.md) | ARM SPE 硬件原理、记录格式、Events 字段与脚本指标映射、内核配置、perf 参数、采样间隔、问题排查 |
| [references/spe_latency_analysis.md](references/spe_latency_analysis.md) | 深度时延分析：标准 CSV schema、指标定义与门禁阈值、时延获取、根因归因方法论、外部 spe-parser 接入约定 |

## 可用脚本

| 脚本 | 用途 |
|------|------|
| `scripts/spe-collect.sh` | SPE 环境检查 + 数据采集（支持 load/store/branch 过滤、采样间隔、时长、PID/CPU 配置） |
| `scripts/spe-parse.sh` | 解析 SPE 采样记录，输出结构化信息（text/json，支持按 sample_count/l1_miss/llc_miss/br_miss 排序和 Top N） |
| `scripts/spe-hotspot.sh` | 热点/瓶颈定位（l1_miss/llc_miss/branch_mispred 指标，百分比阈值过滤，函数聚合，SEVERE/MODERATE/MILD/OK 分级） |
| `scripts/spe-compare.sh` | 优化前后对比（l1_miss/llc_miss/branch_mispred/all 指标 diff 报告，IMPROVED/REGRESSED/NO CHANGE 判定，text/json 格式；函数级对齐支持 Rust mangled 符号去 hash 规范化，函数级判定遵循 -m 指标、all 时任一指标恶化即 REGRESSED，JSON 函数条目含 verdict_metric） |
| `scripts/spe-csv.sh` | 深度分析数据入口：SPE 数据解析为标准 CSV（内置 perf 解析器，`--parser` 接入外部开源 spe-parser，`--validate` 校验 schema，`--schema` 查看字段说明） |
| `scripts/spe-latency-report.py` | 深度时延分析与报告（时延分布/缓存命中层级/指令追逐/高时延根因/门禁判定/前后对比；Markdown + JSON 输出） |
