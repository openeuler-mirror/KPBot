# memory — 内存带宽与命中率指南

## 概述

基于 PMU 事件，采集系统级内存访问带宽和 Cache 命中率，评估内存子系统整体健康状况。与 top-down（宏观定性）和 miss（函数级归因）构成三级分析层次：top-down 发现瓶颈类型，memory 量化系统级带宽和命中率，miss 定位到具体函数。

## 命令

```bash
devkit tuner memory [-h] [-d <sec>] [-l {0,1,2,3}] [-i <sec>] [-m {1,2,3,4}] [-P {100,1000}] [-c {n | n,m | n-m}]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-d <sec>` | 采集时长（秒），最小值 1 | 持续直到 Ctrl+C |
| `-l {0-3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error | 1 |
| `-i <sec>` | 采样间隔（秒），生成子报告，需 ≤ 采集时长 | 采集时长 |
| `-m {1-4}` | 采集模式（见下表） | 1 (ALL) |
| `-P {100,1000}` | 采集周期（毫秒），需 ≤ 采样时长的一半 | 1000（时长 1 秒时为 100） |
| `-c <cpu>` | CPU 核列表：`n`、`n,m`、`n-m` | 全部 |

## 采集模式

| 模式 | 名称 | 分析内容 |
|------|------|---------|
| 1 | ALL | Cache + DDR + HBM 全部 |
| 2 | Cache | L1/L2/L3/TLB 带宽与命中率 |
| 3 | DDR | 每 NUMA 每 DDRC 读写带宽 |
| 4 | HBM | HBM 带宽 |

## 输出结构

### System Information

内核版本、CPU 型号、NUMA 拓扑。

### Cache Miss 概览

```
L1D         2.46%
L1I         2.59%
L2D        35.87%
L2I        53.05%
```

### DDR Bandwidth（系统级）

```
ddrc_write        1285.95MB/s
ddrc_read         3716.89MB/s
```

### L1/L2/TLB 带宽与命中率

按 CPU 聚合，格式：带宽 | 命中率：

```
  CPU       L1D                  L1I                  L2D                  L2I
  all    553305.38MB/s|97.54%  658673.75MB/s|97.41%  67508.30MB/s|64.13%  14808.17MB/s|46.95%
```

### L3 Read 带宽与命中率

按 NUMA/CCL 聚合：

```
  NODE    CCL     Read Hit Bandwidth    Read Bandwidth    Read Hit Rate
  0       --            8437.71MB/s       8420.91MB/s          100.20%
  0       0              1672.84MB/s       1725.49MB/s           96.95%
```

### DDRC 带宽

按 NUMA 每控制器，格式：DDR 读 | DDR 写：

```
  NODE      DDRC_0              DDRC_1              Total
  0    399.75|156.79MB/s   92.25|35.58MB/s    1629.20|853.40MB/s
```

> DDR Read Bandwidth Bottleneck: 40000MB/s（参考值，超过将显著增加延迟）

## 结果解读

| 指标 | 异常值 | 优化方向 |
|------|--------|----------|
| L2D Miss > 30% | L2 缓存效率低 | 改善数据局部性，减小工作集 |
| L2I Miss > 30% | 指令缓存压力 | 代码体积缩减，函数拆分 |
| DDR 读带宽 > 40GB/s | 接近带宽瓶颈 | 减少内存访问，增大缓存利用率 |
| L3 命中率 < 90% | L3 缓存效果差 | 数据布局优化，NUMA 亲和性 |
| DDRC 带宽不均 | 内存访问偏斜 | NUMA 绑定，均衡内存分配 |

## 约束

- 无 OS 版本限制
- 无虚拟机/容器限制
- 仅 aarch64 架构
- 普通用户：需 `perf_event_paranoid=-1` 和 `kptr_restrict=0`

> 注意：memory 命令在 PMU 事件不可用时仍返回退出码 0，需检查输出是否含 "print failed" 或 "N/A" 来判断数据有效性。

## 示例

```bash
# 全量采集 30 秒
bash scripts/run_devkit.sh tuner memory -d 30 -m 1

# 仅 Cache 带宽与命中率
bash scripts/run_devkit.sh tuner memory -d 30 -m 2

# 仅 DDR 带宽
bash scripts/run_devkit.sh tuner memory -d 30 -m 3

# 指定 CPU 核
bash scripts/run_devkit.sh tuner memory -d 30 -c 0-3

# 每秒子报告
bash scripts/run_devkit.sh tuner memory -d 30 -i 1
```
