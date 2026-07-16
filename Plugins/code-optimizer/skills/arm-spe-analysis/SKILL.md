---
name: arm-spe-analysis
description: >
  Use when needing ARM SPE (Statistical Profiling Extension) sampling on aarch64 —
  arm_spe keyword mentioned, traditional perf record precision insufficient,
  instruction-level latency or cache behavior analysis needed, microarchitecture
  bottleneck suspected (high latency, cache miss, branch mispredict, TLB miss)
---

# ARM SPE Analysis — 指令级性能分析

> ARM SPE 硬件统计采样扩展的性能分析技能，覆盖环境检查、数据采集、解析、热点定位、前后对比的完整工作流。

## 角色设定

你是一位 ARM 微架构性能分析专家，精通 SPE 采样机制和微架构瓶颈诊断。核心原则：

- **先检查环境再采集** — SPE 需要硬件支持 + 内核配置 + perf 版本三者缺一不可
- **采样是统计性的** — 采样间隔影响精度和开销，需要根据场景权衡
- **SPE 看微架构，perf 看宏观** — 指令级延迟/Cache/分支用 SPE，函数热点/火焰图用传统 perf

---

## 方法论（4 步法）

```text
1. 环境检查 → spe-collect.sh --check
2. 数据采集 → spe-collect.sh [选项]
3. 数据解析 → spe-parse.sh [选项] <perf.data>
4. 热点定位 → spe-hotspot.sh [选项] <perf.data>
```

优化前后对比：`spe-compare.sh <before.data> <after.data>`

> **核心指标模型**：脚本基于事件类型（event-type）而非延迟值分析 — 主要指标为 **l1_miss**（L1 Cache 未命中率）、**llc_miss**（LLC 未命中率）、**branch_mispred**（分支预测失败率），均以百分比衡量。

---

## 快速诊断决策树

```text
性能分析需求
├── 需要 L1/L2/LLC Cache miss 热点？ → SPE（-m l1_miss / -m llc_miss）
├── 需要分支预测失败分析？ → SPE（-m branch_mispred）
├── 需要 Remote/TLB 访问统计？ → SPE（--summary 查看）
├── 需要按函数聚合微架构指标？ → SPE（--by-function）
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

## 瓶颈识别表

| SPE 指标 | 热点脚本 -m | 瓶颈类型 | 阈值参考 | 典型原因 |
|----------|------------|---------|---------|---------|
| l1_miss > 30% | `l1_miss` | 严重 L1 Cache 未命中 | >30% SEVERE, >10% MODERATE, >5% MILD | 访存模式不友好、false sharing |
| llc_miss > 30% | `llc_miss` | 严重 LLC 未命中 | >30% SEVERE, >10% MODERATE, >5% MILD | 工作集超过 L3 容量、NUMA 跨节点 |
| branch_mispred > 20% | `branch_mispred` | 严重分支预测失败 | >20% SEVERE, >5% MODERATE, >1% MILD | 分支模式不可预测、多态调用 |
| remote_access 比例高 | `llc_miss` | NUMA 跨节点访问 | 解析汇总中 Remote 占比高 | 内存未绑 NUMA 节点 |
| tlb_miss 持续出现 | `llc_miss` | 页表遍历开销 | 解析汇总中 TLB miss 占比高 | 大内存遍历、页面碎片化 |

> **阈值判定**：`spe-hotspot.sh` 内置 classify_bottleneck 函数自动标注 SEVERE/MODERATE/MILD/OK 等级。

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

# JSON 格式
bash scripts/spe-compare.sh -f json before.data after.data
```

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

---

## 参考文档索引

| 文档 | 内容 |
|------|------|
| [references/spe_guide.md](references/spe_guide.md) | ARM SPE 硬件原理、记录格式、Events 字段与脚本指标映射、内核配置、perf 参数、采样间隔、问题排查 |

## 可用脚本

| 脚本 | 用途 |
|------|------|
| `scripts/spe-collect.sh` | SPE 环境检查 + 数据采集（支持 load/store/branch 过滤、采样间隔、时长、PID/CPU 配置） |
| `scripts/spe-parse.sh` | 解析 SPE 采样记录，输出结构化信息（text/json，支持按 sample_count/l1_miss/llc_miss/br_miss 排序和 Top N） |
| `scripts/spe-hotspot.sh` | 热点/瓶颈定位（l1_miss/llc_miss/branch_mispred 指标，百分比阈值过滤，函数聚合，SEVERE/MODERATE/MILD/OK 分级） |
| `scripts/spe-compare.sh` | 优化前后对比（l1_miss/llc_miss/branch_mispred/all 指标 diff 报告，IMPROVED/REGRESSED/NO CHANGE 判定，text/json 格式） |
