# nmon — 系统性能数据采集指南

## 概述

nmon（Nigel's Performance Monitor）采集 CPU、内存、磁盘 IO、网络等系统级性能数据，输出 CSV 格式（`.nmon` 文件），对应 app-tuner 中 mpstat/iostat/vmstat/sar/free 的采集能力。

## 采集命令

```bash
bash scripts/collect.sh [-d <sec>] [-i <sec>] [--top] [--top-args] [--snapshot] [--json] [-m <dir>]
```

| 参数 | 说明 | 默认值 | 映射到 nmon |
|------|------|--------|------------|
| `-d <sec>` | 采集时长（秒） | 10 | `-c`（快照数 = ceil(d/i)，向上取整） |
| `-i <sec>` | 采样间隔（秒） | 1 | `-s <sec>` |
| `--top` | 附带 Top Processes 统计 | 关闭 | `-t` |
| `--top-args` | Top Processes 含命令行参数 | 关闭 | `-T`（含 `-t` 行为） |
| `--snapshot` | 单次快照 | - | `-s 1 -c 1` |
| `--json` | JSON envelope 包装输出 | 关闭 | - |
| `-m <dir>` | 数据文件保留目录 | 临时文件（用后删除） | `-m <dir>` |

## 数据文件

collect.sh 以 `nmon -p -F <file>.nmon` 启动后台采集并等待完成后输出（nmon `-f/-F` 会 fork 到后台立即返回，直接读文件会截断）。文件名 `nmon_<YYYYMMDD>_<HHMMSS>_<pid>.nmon`。collect.sh 默认写到临时目录、输出后删除；`-m` 或 `--json` 时保留（stderr/`data_file` 报告路径）。

## CSV 数据段（按域解读）

| 段 | 含义 | 关键指标 |
|----|------|---------|
| `AAA`/`BBB` | 元数据 | 命令、版本、主机名、采集参数 |
| `ZZZZ` | 时间戳 | 每个快照的时间 |
| `CPU001`~`CPUnnn` | 每核利用率 | User%、Sys%、Wait%、Idle% |
| `CPU_ALL` | 全核平均 | User%、Sys%、Wait%、Idle%，CPU 占用分解 |
| `MEM` | 内存 | memtotal、swaptotal、memfree、cached、active、buffers |
| `VM` | 虚拟内存/页 | pgmin/s、pgout/s、 faults、swapin/out |
| `DISKBSIZE` | 磁盘块大小 | 每设备每秒传输块数 |
| `DISKBUSY` | 磁盘繁忙度 | 每设备繁忙 %（对应 iostat %util） |
| `DISKREAD`/`DISKWRITE` | 磁盘读写吞吐 | 每设备 KB/s |
| `DISKXFER` | 磁盘 IOPS | 每设备每秒传输次数 |
| `NET` | 网络吞吐 | 每接口 read/write KB/s |
| `NETPACKET` | 网络包 | 每接口 packets/s |
| `PROC` | 进程 | pswitches/s（上下文切换）、pswitch、lcpu/fork |
| `HUGEPAGES` | 大页 | Total/Free/Reserve pages |
| `JFSFILE` | 文件系统 | 每文件系统使用 % |
| `TOP`（`--top`） | 进程排名 | PID、CPU%、命令（`--top-args` 含完整命令行） |

## 结果解读

| 指标 | 异常信号 | 优化方向 |
|------|---------|---------|
| `CPU_ALL` Sys% > 30% | 内核态开销大 | 检查系统调用、锁竞争、中断 |
| `CPU_ALL` Wait% > 20% | IO 等待明显 | 结合 DISKBUSY 定位慢盘 |
| `DISKBUSY` 持续 > 80% | 磁盘饱和 | 换盘/分盘/调 I/O scheduler |
| `NET` 接口接近链路带宽 | 网络饱和 | 多队列/带宽扩容（结合 kptools-network-collector） |
| `MEM` memfree 持续很低且 swapin/out > 0 | 内存不足 | 限流/扩容/大页调整 |
| `PROC` pswitches/s 异常高 | 上下文切换频繁 | 线程数/绑核（结合 kptools-proc-collector） |

## 工具分工

| 采集需求 | 工具 |
|---------|------|
| 系统级资源基线（CPU/内存/磁盘/网络） | kptools-nmon（本 SKILL） |
| 微架构/topdown/cache miss | kptools-devkit |
| 单进程线程-CPU 分布/上下文切换 | kptools-proc-collector |
| 网络深度（丢包/重传/队列/IRQ） | kptools-network-collector |

## 分析工具

保留的 `.nmon` 文件可用 nmonanalyser / nmonchart 生成图形报告（本 SKILL 不做解析，`--json` 仅包装不转换）。

## 约束

- nmon `-f/-F` 为后台 fork 模式，collect.sh 负责同步等待（`-p` PID + `tail --pid`），交互模式被禁用
- 快照数过大导致数据文件过大（建议不超过 800 快照）
- 实际耗时 = 采集时长 + BBBP 基线时间：nmon 启动时 shell out 执行 ifconfig 等命令采集基线，接口数极多的宿主机（如近千 veth 的容器宿主机）基线可达分钟级，`-d 1` 也可能等待数分钟，属 nmon 自身行为；中断时 collect.sh 会清理临时文件
