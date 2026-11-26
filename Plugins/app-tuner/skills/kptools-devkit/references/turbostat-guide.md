# turbostat — core/uncore 频率与功耗采集指南

## 概述

基于 PMU 事件采集 CPU 核频率、uncore（L3 Cache）频率、Socket 功耗和温度数据。带 `--bmc` 参数可获取整机功耗、内存功耗和进出风口温度等带外数据。

## 命令

```bash
devkit tuner turbostat [-h] [-l {0,1,2,3}] [-d <sec>] [-i <sec>] [-c <cpu>] [--bmc]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-l {0,1,2,3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error | 1 |
| `-d <sec>` | 采集时长（秒），最小值 1。默认持续采集，可用 Ctrl+\ 取消或 Ctrl+C 停止 | 持续 |
| `-i <sec>` | 采样间隔（秒），设置后生成子报告，需 ≤ 采集时长 | 不生成子报告 |
| `-c <cpu>` | CPU 核列表：`n`、`n,m`、`n-m`、`l,m-n` | 全部 |
| `--bmc` | 启用 BMC 交互输入（BMC HOST IP, BMC USER, BMC PASSWORD），获取带外数据 | 关闭 |

## 输出表

### 1. Per NUMA Frequency Table

按 NUMA 节点聚合的 CPU 和 Uncore 频率：

```
| NUMA ID | CPU Frequency (MHz) | Uncore Frequency (MHz) |
|       0 |              2300.0 |                1767.52 |
|       1 |              2300.0 |                1767.74 |
|       2 |              2300.0 |                 1730.8 |
|       3 |              2300.0 |                1729.97 |
```

### 2. CPU Core Frequency Table

每逻辑核频率：

```
| Logical ID | Physical ID | NUMA ID | Frequency (MHz) |
|          0 |           0 |       0 |          2300.0 |
|          1 |           0 |       0 |          2300.0 |
|          2 |           1 |       0 |          2300.0 |
```

- Logical ID = CPU 线程索引（192 核 × 2 线程系统上为 0-383）
- Physical ID = 物理核编号

### 3. Uncore(L3 Cache) Frequency Table

每个 L3 Cache 设备的频率：

```
| Uncore Device(L3 Cache) Name | NUMA ID | Frequency (MHz) |
| hisi_sccl0_l3c0_0            |       0 |         1767.74 |
| hisi_sccl0_l3c0_1            |       0 |         1767.13 |
```

设备命名规则：`hisi_sccl{socket}_{l3c_instance}_{slice}`

### 4. CPU Socket Power and Temperature Table（带内）

```
| CPU Socket ID | CPU Socket Power (W) | CPU Socket Die0 Temperature (C) | CPU Socket Die1 Temperature (C) |
|             0 |               111.34 |                            50.1 |                            49.5 |
|             1 |               129.00 |                            54.8 |                            52.2 |
```

### 5. 带外表（需要 --bmc）

- CPU Socket Temperature Table（带外温度）
- Server Status Table：总功耗/CPU 功耗/内存功耗/进风口温度/出风口温度

## 子报告

设置 `-i` 后，每个采样间隔生成一个子报告。最终汇总报告聚合所有间隔的数据。

## 结果解读

| 指标 | 异常值 | 含义 |
|------|--------|------|
| CPU 频率 = 最大值 | 无降频 | 正常 |
| CPU 频率 < 最大值 | 降频 | 频率调节策略或散热问题 |
| Uncore 频率差异大 | 负载不均 | 不同 NUMA 节点负载差异 |
| Socket 功耗低但利用率高 | 空闲功耗浪费 | 电源管理优化空间 |
| Die 温度 > 85°C | 散热问题 | 需关注散热 |
| 整机功耗 > PSU 额定 80%（--bmc） | 电源过载风险 | 检查 PSU 冗余配置 |
| 进风口温度 > 35°C（--bmc） | 环境温度过高 | 检查机房制冷 |

> 注意：部分环境下 CPU 频率可能以 GHz 而非 MHz 显示（如 2.9 表示 2.9 GHz = 2900 MHz）。

## 约束

- 无 OS 版本限制
- 无虚拟机/容器限制
- 仅 aarch64 架构
- `--bmc` 为交互式参数，需手动输入 BMC IP/用户名/密码，不适用于自动化场景

## 示例

```bash
# 采集 3 秒，每秒一个子报告
bash scripts/run_devkit.sh tuner turbostat -d 3 -i 1

# 仅监控指定 CPU 核
bash scripts/run_devkit.sh tuner turbostat -d 10 -i 2 -c 0-15

# 带 BMC 带外数据
bash scripts/run_devkit.sh tuner turbostat -d 30 --bmc
```
