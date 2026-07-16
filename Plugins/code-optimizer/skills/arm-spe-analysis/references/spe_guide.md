# ARM SPE 深度参考指南

## 目录
1. [SPE 硬件原理](#1-spe-硬件原理)
2. [SPE 记录格式](#2-spe-记录格式)
3. [Events 字段与脚本指标映射](#3-events-字段与脚本指标映射)
4. [内核驱动配置](#4-内核驱动配置)
5. [perf arm_spe 参数详解](#5-perf-arm_spe-参数详解)
6. [采样间隔选择指南](#6-采样间隔选择指南)
7. [常见问题排查](#7-常见问题排查)

---

## 1. SPE 硬件原理

ARM Statistical Profiling Extension (SPE) 是 ARMv8.2 架构引入的硬件统计采样扩展。

### 采样机制

SPE 在 CPU 流水线中对指令进行随机采样，记录每条采样指令的完整执行信息：

- **采样率**：由 PMSIDR_EL1.Interval 设置，表示每 N 个操作采样一次
- **无中断开销**：SPE 将采样记录写入内存缓冲区，不产生 PMI 中断
- **统计性**：采样是随机的，非每条指令都记录

### 关键寄存器

| 寄存器 | 作用 |
|--------|------|
| PMSIDR_EL1 | SPE 实现标识（版本、间隔范围、支持的 filter 等） |
| PMSIRR_EL1 | 采样间隔控制 |
| PMSFCR_EL1 | Filter 控制（load/store/branch） |
| PMSEVFR_EL1 | Event filter（Cache hit/miss、TLB、分支预测等） |
| PMSLATFR_EL1 | 延迟过滤（仅记录延迟 > N 的采样） |
| PMBLIMITR_EL1 | 缓冲区限制寄存器 |

### 与传统 PMU 采样的区别

| 特性 | 传统 PMU (perf record) | SPE |
|------|----------------------|-----|
| 采样方式 | 计数器溢出中断 | 硬件随机采样 |
| 开销 | 中断处理开销 | 无中断，写缓冲区 |
| 信息量 | PC + 调用栈 | PC + 延迟 + Cache + TLB + 分支 |
| 精度 | 有 skid（实际指令和采样点偏差） | 低 skid |
| 适用场景 | 宏观热点、火焰图 | 微架构瓶颈、指令级延迟 |

---

## 2. SPE 记录格式

每条 SPE 采样记录由多个 packet 组成：

### Packet 类型

| Packet | 含义 | 关键字段 |
|--------|------|---------|
| Address | 采样指令的虚拟地址 | PC 值 |
| Context | 上下文信息 | EL、NS、Cond |
| Counter | 事件计数器 | Instructions、CPU cycles |
| Events | 微架构事件 | Cache/TLB/分支预测状态 |
| Data Source | 数据来源 | L1/L2/L3/远端内存 |
| Latency | 指令延迟 | 总延迟周期数 |
| Operation | 指令类型 | Load/Store/Branch/Other |

### Events Packet 字段详解

```
Events 字段位分布:
- [0]   SPEEVCYCLE_ISSUE_INSN  — 指令已发射
- [1]   SPEEVCYCLE_SCALAR      — 标量操作
- [2]   SPEEVCYCLE_SVE         — SVE 操作
- [3]   SPEEVCYCLE_PROBE       — 探测操作
- [4]   SPEEVCYCLE_FP          — 浮点操作
- [5]   SPEEVCYCLE_DEC         — 解码操作
- [6]   SPEEVCYCLE_BR_PRED     — 分支预测正确
- [7]   SPEEVCYCLE_MISPRED     — 分支预测失败
- [8]   SPEEVCYCLE_STL_MISS    — Store 指令 L1 miss
- [9]   SPEEVCYCLE_L1D_MISS    — L1 Data Cache miss
- [10]  SPEEVCYCLE_L1D_ACCESS  — L1 Data Cache access
- [11]  SPEEVCYCLE_L2_MISS     — L2 Cache miss
- [12]  SPEEVCYCLE_L2_ACCESS   — L2 Cache access
- [13]  SPEEVCYCLE_LLC_MISS    — Last-Level Cache miss
- [14]  SPEEVCYCLE_LLC_ACCESS  — Last-Level Cache access
- [15]  SPEEVCYCLE_TLB_MISS    — TLB miss
- [16]  SPEEVCYCLE_TLB_ACCESS  — TLB access
```

### Data Source 编码

| 编码 | 含义 |
|------|------|
| 0x0 | Unknown |
| 0x1 | L1 Data Cache |
| 0x2 | L2 Cache |
| 0x3 | L3 Cache (本地) |
| 0x4 | Peer Cache (同 socket) |
| 0x5 | 远端 Cache |
| 0x6 | DDR 内存 (本地) |
| 0x7 | DDR 内存 (远端) |

---

## 3. Events 字段与脚本指标映射

`perf script` 输出的 SPE 记录中，每行的事件类型（EVENT_TYPE）来自 Events Packet 的对应位。脚本基于事件类型进行统计和聚合，而非延迟值。

### perf script 输出格式

```
COMM PID [CPU] TIMESTAMP: PERIOD EVENT_TYPE: IP SYMBOL (DSO)
```

示例：
```
mysqld 1234 [00] 1234.567: 1 l1d-miss: ffff8000 dead_loop (/usr/bin/myapp)
mysqld 1234 [00] 1234.568: 1 llc-miss: ffff8010 hot_func (/usr/bin/myapp)
mysqld 1234 [00] 1234.569: 1 branch-miss: ffff8020 branch_site (/usr/bin/myapp)
```

### Events Packet 位 → perf 事件类型 → 脚本指标

| Events Packet 位 | perf script EVENT_TYPE | spe-parse.sh 统计 | spe-hotspot.sh -m | spe-compare.sh -m |
|-------------------|------------------------|-------------------|-------------------|-------------------|
| [9] SPEEVCYCLE_L1D_MISS | `l1d-miss` | l1_miss 计数+百分比 | `l1_miss` | `l1_miss` |
| [13] SPEEVCYCLE_LLC_MISS | `llc-miss` | llc_miss 计数+百分比 | `llc_miss` | `llc_miss` |
| — (无固定位) | `remote-access` | remote_access 计数+百分比 | — (含在 llc_miss 中) | — |
| [16] SPEEVCYCLE_TLB_ACCESS | `tlb-access` | tlb_access 计数 | — | — |
| [15] SPEEVCYCLE_TLB_MISS | `tlb-miss` | tlb_miss 计数+百分比 | — | — |
| [7] SPEEVCYCLE_MISPRED | `branch-miss` | branch_miss 计数+百分比 | `branch_mispred` | `branch_mispred` |

### 指标体系说明

脚本采用**事件类型（event-type）**分析模型，核心指标均为百分比：

- **l1_miss**：L1 Data Cache 未命中事件占总采样记录的比例，反映数据局部性
- **llc_miss**：Last-Level Cache 未命中事件占总采样记录的比例，反映工作集与 Cache 容量关系
- **branch_mispred**：分支预测失败事件占总采样记录的比例，反映分支可预测性
- **remote_access**：远端访问事件，统计在汇总中，热点定位归入 llc_miss 维度
- **tlb_access / tlb_miss**：TLB 访问与未命中，统计在汇总中，不作为独立热点指标

### 阈值分级标准

`spe-hotspot.sh` 内置的 classify_bottleneck 函数使用以下阈值：

| 指标 | SEVERE | MODERATE | MILD | OK |
|------|--------|----------|------|----|
| l1_miss (%) | > 30 | > 10 | > 5 | ≤ 5 |
| llc_miss (%) | > 30 | > 10 | > 5 | ≤ 5 |
| branch_mispred (%) | > 20 | > 5 | > 1 | ≤ 1 |

---

## 4. 内核驱动配置

### 必需内核配置

```bash
# 检查内核是否启用 SPE PMU
grep CONFIG_ARM_SPE_PMU /boot/config-$(uname -r)

# 必需配置项
CONFIG_ARM_SPE_PMU=y
CONFIG_PERF_EVENTS=y
```

### 动态加载（如果编译为模块）

```bash
# 查找 SPE 模块
find /lib/modules/$(uname -r) -name "*spe*"

# 加载模块
sudo modprobe arm_spe_pmu
```

### 常用内核参数

```bash
# 允许非 root 用户使用 perf
sudo sysctl -w kernel.perf_event_paranoid=0

# 永久生效
echo "kernel.perf_event_paranoid=0" | sudo tee -a /etc/sysctl.d/99-perf.conf
sudo sysctl --system
```

---

## 5. perf arm_spe 参数详解

### 基本语法

```bash
perf record -e arm_spe/<参数>/ ...
```

### Filter 参数

| 参数 | 值 | 含义 |
|------|---|------|
| load | 0/1 | 采样 load 指令 |
| store | 0/1 | 采样 store 指令 |
| branch | 0/1 | 采样分支指令 |
| min_interval | N | 最小采样间隔（PMSIDR_EL1.Interval 值） |

### 常用组合

```bash
# 全部采样（默认）
perf record -e arm_spe// -a sleep 10

# 仅采样 load 指令（内存读取分析）
perf record -e arm_spe/load=1/ -a sleep 10

# 仅采样 store 指令（写入分析）
perf record -e arm_spe/store=1/ -a sleep 10

# 仅采样分支指令（分支预测分析）
perf record -e arm_spe/branch=1/ -a sleep 10

# Load + Store（内存访问分析）
perf record -e arm_spe/load=1,store=1/ -a sleep 10

# 指定采样间隔（更精确，开销更大）
perf record -e arm_spe/min_interval=0/ -a sleep 10
```

### 查看 SPE 支持的参数

```bash
# 列出 SPE 事件
perf list | grep -A5 arm_spe

# 查看 SPE PMU 设备信息
ls /sys/bus/event_source/devices/arm_spe_0/

# 查看最小采样间隔
cat /sys/bus/event_source/devices/arm_spe_0/min_interval

# 查看事件类型号
cat /sys/bus/event_source/devices/arm_spe_0/type
```

---

## 6. 采样间隔选择指南

采样间隔决定了 SPE 多久采样一次指令。间隔越小，采样越精确，但开销和缓冲区占用越大。

### 间隔参考表

| 间隔值 | 采样密度 | 开销 | 适用场景 |
|--------|---------|------|---------|
| 0 (最小) | 最高 | 高 | 短时间精确分析、关键路径延迟诊断 |
| min_interval × 1 | 高 | 中高 | 通用微架构瓶颈分析 |
| min_interval × 10 | 中 | 低 | 常规性能分析（推荐默认值） |
| min_interval × 100 | 低 | 极低 | 长时间监控、低开销采样 |

### 选择建议

1. **首次分析**：使用 min_interval × 10（脚本默认），平衡精度和开销
2. **瓶颈定位后深入**：缩小到 min_interval × 1 或更小，获取更多采样
3. **长时间采集**：放大间隔到 min_interval × 100，避免缓冲区溢出
4. **指定 CPU 采集**：可以缩小间隔，因为只采集部分 CPU

### 缓冲区大小调整

```bash
# 默认缓冲区页数（每页 4KB）
# 采样密度高时需要增大缓冲区
perf record -e arm_spe// --buf-size 128M -a sleep 10

# 或通过 sysctl 调整
sudo sysctl -w kernel.perf_event_mlock_kb=2048
```

---

## 7. 常见问题排查

### 采集无数据

**现象**：`perf script` 输出为空或记录数为 0

**排查步骤**：

```bash
# 1. 确认 SPE 硬件支持
bash scripts/spe-collect.sh --check

# 2. 确认 perf 版本（需要 5.10+ 内核对应的 perf）
perf --version

# 3. 确认权限
cat /proc/sys/kernel/perf_event_paranoid
# 如果 > 1，需要 root 或调低

# 4. 手动测试采集
sudo perf record -e arm_spe// -a sleep 1 -o /tmp/test.data
perf script -i /tmp/test.data | head -5
```

### 数据量过小

**现象**：采样记录很少，统计意义不足

**解决方案**：
- 缩小采样间隔（`-i 0` 或更小值）
- 延长采集时间（`-t` 参数）
- 减小 filter 范围（如只采集 load）

### perf 版本不兼容

**现象**：`perf record` 报错 "event syntax error" 或 "invalid or unsupported event"

**解决方案**：
```bash
# 检查 perf 与内核版本是否匹配
perf --version
uname -r

# 如果不匹配，编译匹配版本的 perf
sudo apt install linux-tools-$(uname -r)   # Debian/Ubuntu
sudo yum install perf                       # RHEL/openEuler
```

### 缓冲区溢出

**现象**：`perf record` 报 "AUX area overflow" 或数据丢失

**解决方案**：
```bash
# 增大 AUX 缓冲区
perf record -e arm_spe// --buf-size 256M -a sleep 10

# 或增大采样间隔减少数据量
perf record -e arm_spe/min_interval=100/ -a sleep 10
```

### 容器环境限制

**现象**：容器内运行 perf 报权限错误

**解决方案**：
```bash
# 需要特权模式或 SYS_ADMIN capability
docker run --privileged ...
# 或
docker run --cap-add SYS_ADMIN ...
```
