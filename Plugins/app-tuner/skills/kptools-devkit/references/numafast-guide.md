# numafast — NUMA 访存分析指南

## 概述

基于 ARM SPE（Statistical Profiling Extension）能力，分析系统精细化的 DDR 访问、
NUMA 访问流量矩阵以及进程的内存访问等信息，帮助定位 NUMA 跨节点访问导致的
内存延迟瓶颈。

## 命令

```bash
devkit tuner numafast [-d <sec>] [-i <sec>] [-o <path>] [-l {0,1,2,3}] [-c <n>] [-n <n>] [-p <PID>] [-t <n>] [--package] [-f]
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-h, --help` | 显示帮助信息 | - |
| `-o, --output <path>` | 报告文件名（不需 .tar 后缀），默认当前路径 `numafast-时间戳.tar` | 当前路径 |
| `-l, --log-level {0-3}` | 日志级别：0=debug, 1=info, 2=warning, 3=error。debug 级别会保存原始 SPE 数据到 `spe_origin.data`，可能占用大量磁盘 | 1 |
| `-d, --duration <sec>` | 采集时长（秒），范围 1~2^31-1。默认持续采集，Ctrl+\ 取消或 Ctrl+C 停止并进入分析 | 持续 |
| `-i, --interval <sec>` | 采样间隔（秒），范围 1~2^31-1。生成子报告，需 ≤ 采集时长 | 5 |
| `-c, --count <n>` | SPE 采集指令间隔，范围 1~2^32-1 | 65536 |
| `-n, --num <n>` | 展示 Top N 进程数，范围 1~100。按进程访存流量排序 | 30 |
| `-t, --threads [n]` | 每个进程的 Top N 线程数，范围 1~10 | 5 |
| `-p, --pid <PID>` | 目标进程 PID（逗号分隔多个） | - |
| `--package` | 导入数据库并生成 .tar 包 | 关闭 |
| `-f, --file` | 不压缩为 .tar 而是输出为文件，需与 `--package` 同时使用 | 关闭 |

## 输出结构

### 1. NUMA 访问流量矩阵

系统 NUMA 评分 + SRC→DST 流量矩阵：

```
1. System's numa score : -0.00
   score = (max cost - real cost) / (max cost - min cost)
   real cost = SUM(numa distance(i, j) * access percentage(i, j))
   格式: traffic | numa distance | access percentage

              DST_0               DST_1               DST_2               DST_3
SRC_0   0.00GB|10|0.00%     0.00GB|15|0.00%     0.00GB|20|0.00%     0.00GB|20|0.00%
SRC_1   0.00GB|15|0.00%     0.00GB|10|0.00%     0.00GB|20|0.00%     0.00GB|20|0.00%
SRC_2   0.00GB|20|0.00%     0.00GB|20|0.00%     0.00GB|10|0.00%     0.00GB|15|0.00%
SRC_3   0.80GB|20|100.00%   0.00GB|20|0.00%     0.00GB|15|0.00%     0.00GB|10|0.00%
```

- NUMA score：最优为 1，最差为 0。基于 NUMA 距离和访问占比加权计算
- 对角线（distance=10）= 本节点访问
- 非对角线 = 跨节点访问，distance 越大延迟越高

### 2. 节点详情

按源节点聚合的内存访问信息：

```
 NID  RMA_Die  RMA_Skt      LMA    %RMA   MEM_all  MEM_free   %MEM      %CPU
   0   0.00GB   0.00GB   0.00GB    0.00  192.44GB  100.91GB  47.56    335.76
   1   0.00GB   0.00GB   0.00GB    0.00  198.17GB  132.19GB  33.29    166.94
   2   0.00GB   0.00GB   0.00GB    0.00  198.13GB  109.84GB  44.56    166.43
   3   0.00GB   0.80GB   0.00GB  100.00  197.10GB  117.49GB  40.39    176.37
```

- RMA_Die：跨 Die 远端访问流量
- RMA_Skt：跨 Socket 远端访问流量
- LMA：本地访问流量
- %RMA：远端访问占比
- %CPU：CPU 核占用率（600% = 6 核被占用）

### 3. Top N 进程/线程

按访存流量排序的进程和线程：

```
    PID  SCORE  ACCESS  RMA_Die  RMA_Skt      LMA    %RMA  MIGRATED    %CPU    COMMAND
3212506   0.00 100.00%   0.00GB   0.80GB   0.00GB  100.00    0|1        --     coder
```

- SCORE：NUMA 评分（0=最差，1=最优）
- ACCESS：进程访存流量占总流量的百分比
- MIGRATED X|Y：X = 线程在 NUMA 节点间迁移次数，Y = 进程线程数
- %RMA：进程远端访问占比

## 结果解读

| 指标 | 异常值 | 优化方向 |
|------|--------|----------|
| NUMA score < 0.5 | 跨节点访问比例高 | 绑核 + 内存亲和性绑定（numactl --membind） |
| %RMA > 30% | 远端内存访问过多 | 将进程绑定到数据所在 NUMA 节点 |
| RMA_Skt 高 | 跨 Socket 访问延迟最大 | 避免跨 Socket 数据共享，调整线程分布 |
| MIGRATED 高 | 线程频繁跨节点迁移 | 绑核固定线程到 NUMA 节点 |
| 某节点 %MEM 高 | 内存分布不均 | 重新分配内存或迁移数据 |

## 子报告

设置 `-i` 后，每个采样间隔生成子报告，末尾输出汇总报告。

## TAR 结果包

加 `--package` 生成 .tar 包，可用 `devkit report -i <file.tar>` 查看：

```bash
bash scripts/run_devkit.sh tuner numafast -d 30 --package
bash scripts/run_devkit.sh report -i numafast-时间戳.tar
```

## 约束

- 需服务器支持 ARM SPE 采集能力
- **不支持虚拟机和容器环境**
- 仅 aarch64 架构
- 需先配置 SPE 环境

## 示例

```bash
# 采集 30 秒，每 5 秒一个子报告
bash scripts/run_devkit.sh tuner numafast -d 30 -i 5

# 指定进程采集
bash scripts/run_devkit.sh tuner numafast -p 1234 -d 30

# 生成 TAR 结果包
bash scripts/run_devkit.sh tuner numafast -d 30 --package

# 展示 Top 10 进程，每进程 Top 3 线程
bash scripts/run_devkit.sh tuner numafast -d 30 -n 10 -t 3
```
