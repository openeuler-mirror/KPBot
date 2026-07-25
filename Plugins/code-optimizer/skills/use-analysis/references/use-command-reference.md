# USE 命令参考与指标解读

本文件给出 USE 分析法四大资源 × U/S/E 三维度的**实战命令、输出解读、阈值、典型故障案例**，以及 60 秒快查流程与常见误区。paste 模式解读用户贴的命令输出、或向用户解释"为什么这么判断"时阅读本文件。`use_scan.py` 的阈值与此处一致。

## 目录

1. [阈值速查表](#阈值速查表)
2. [CPU 资源](#cpu-资源)
3. [内存资源](#内存资源)
4. [磁盘IO 资源](#磁盘io-资源)
5. [网络资源](#网络资源)
6. [60 秒极速排查流程](#60-秒极速排查流程)
7. [常见误区](#常见误区)
8. [USE vs 其他性能方法](#use- vs-其他性能方法)

## 阈值速查表

脚本内置阈值（经验值，`status_for` 判定 ok/warn/crit）。带 † 的指标与负载特征强相关，需结合业务基线判断。

| 资源 | 维度 | 指标 | warn | crit |
|------|------|------|------|------|
| CPU | U | us+sy 平均占比 | 80% | 90% |
| CPU | U | 单核峰值占比 | 80% | 90% |
| CPU | S | 运行队列 r / nproc † | 0.7 | 1.0 |
| CPU | S | 1min 负载 / nproc † | 0.7 | 1.0 |
| CPU | S | 每核每秒上下文切换 cs/nproc † | 5000 | 15000 |
| 内存 | U | available / total | <10% | <5% |
| 内存 | S | swap si 或 so (KB/s) | >0 | >1024 |
| 磁盘 | U | %util | 80% | 95% |
| 磁盘 | U | 文件系统容量使用率 | 90% | 95% |
| 磁盘 | S | await (ms) | 20 | 50 |
| 磁盘 | E | 挂载卡死 / Remote I/O error | 0 | ≥1 |
| 网络 | U | %ifutil | 70% | 90% |
| 网络 | S | TIME_WAIT 连接数 † | 5000 | 20000 |
| 网络 | E | 重传率 % | 0.5% | 2.0% |
| 网络 | E | InErrs 增量 | >0 | >0 |
| 各资源 | E | dmesg/journalctl 匹配 | 0 条 | ≥1 条 |

## CPU 资源

CPU 是最常见瓶颈，优先排查。

### U 使用率

```bash
vmstat 1 5              # 全局 CPU 动态，看 us/sy/id/wa
mpstat -P ALL 1 5       # 每核使用率，定位单核打满
pidstat 1 5             # 按进程看 CPU 占用
top -b -n 1 -o %CPU | head -15   # TOP 进程（脚本无 root 时 pidstat 也可用）
```

解读：
- `vmstat` 的 `us`（用户）+`sy`（系统）是总使用率；`id`（idle）= 100 - 使用率。**但很多人忽略 `cs`（上下文切换）和 `in`（中断）**--见饱和度。
- `mpstat -P ALL` 看每核：单核打满（某核 %idle=0）即使整体使用率不高也会卡（单线程瓶颈 / 中断绑核）。
- 注意 `wa`（iowait）：高 wa 说明 CPU 在等 IO，**根因在磁盘不在 CPU**，别去优化 CPU。

### S 饱和度

```bash
uptime                  # 1/5/15min 负载
vmstat 1 | grep -E "procs|r|b"   # 持续看 r（运行队列）、b（阻塞进程）
ps -eo stat | grep R | wc -l     # 当前运行队列进程数
```

解读：
- **`r` > CPU 核数 = CPU 饱和**，进程排队等调度，直接卡顿。4 核机器 r 长期 6-10，即使使用率仅 60% 也卡。
- **负载（load average）> 核数 = 饱和**。注意负载含 D 状态（不可中断睡眠，常是等 IO），负载高但 CPU 闲，根因可能在磁盘。
- **`cs`（上下文切换）爆炸**：每秒几万甚至十几万。Java 线程频繁创建销毁、锁竞争会导致 CPU 使用率不高但 cs 爆炸，接口超时。**必须按核数归一化**（cs/nproc）：192 核上 10 万 cs/秒正常（~520/核），4 核上 10 万就严重（25000/核）。
- 脚本对 r、负载、cs 均按 nproc 归一化，避免大核数机器误判。
- **辅助信号 `wa`/`st`/`b`/`in`**（脚本一并采集并标记）：`wa`(iowait) 高 = CPU 在等盘，根因在磁盘不在 CPU，别去优化 CPU；`st`(steal) > 0 = 虚机被宿主偷 CPU（云上常见，裸机为 0）；`b`(D 状态阻塞进程) > 0 = 有进程卡在不可中断睡眠，通常等 IO，与磁盘瓶颈互印证；`in`(中断) 异常高 = 中断风暴（如网卡中断绑单核）。

### E 错误率

```bash
dmesg | tail            # 内核报错（需 root 或在 systemd-journal/adm 组）
dmesg | grep -iE "mce|hardware error|thermal|throttl"   # CPU 硬件报错
journalctl -k --no-pager -n 200 | grep -i error          # dmesg 无权限时
```

故障案例：服务器偶尔卡顿、重启恢复、无高负载，大概率是 CPU 硬件报错（MCE）、过热降频或内核调度异常。脚本把 dmesg 中 cpu/system 类匹配归到 CPU 错误率。

## 内存资源

内存问题引发隐性卡顿、闪退、OOM 被杀。

### U 使用率

```bash
free -m                 # total/used/free/shared/buff/cache/available
cat /proc/meminfo       # 缓存、缓冲区详情
ps -aux --sort=-%mem | head -10   # 内存占用 TOP 进程
```

解读（**最重要的坑**）：
- **看 `available` 不看 `free`**！`free` 低但 `available` 充足是**正常**现象--文件缓存占用大量内存，但可回收。新手看 `free` 趋近 0 就以为内存不足，误判。
- `available` < 总量 10% 才 warn，<5% crit。脚本按 available 判定。
- `shared` 高（tmpfs/IPC）需单独关注，不归入普通使用率。

### S 饱和度

```bash
vmstat 1 5     # 看 si（swap 换入）、so（swap 换出）
```

解读：`si`/`so` 持续非 0 = 物理内存不足，系统频繁换页，性能暴跌。这是内存饱和的硬指标。脚本对 si/so 任一 >0 即 warn，>1024 KB/s crit。

### E 错误率

```bash
dmesg | grep -i oom
journalctl -k | grep -iE "oom|killed process|page allocation failure"
```

故障场景：业务突然宕机、进程消失、无应用日志报错，90% 是 OOM 被内核杀。脚本把 oom/killed process/page allocation failure 归到内存错误率。看到 OOM 记录要查是哪个进程被杀、为什么吃内存（泄漏 vs 合理增长）。

## 磁盘IO 资源

CPU/内存正常但服务卡，90% 是磁盘 IO 瓶颈，数据库、日志服务尤甚。

### U 使用率

```bash
iostat -x 1 5           # %util、await、r/s、w/s、rkB/s、wkB/s
iostat -d 1             # 读写吞吐量
df -h                   # 各文件系统容量使用率（Use%，与 %util 是两码事）
```

解读：
- **`%util` 持续 ≥95% = 磁盘 IO 打满**。MySQL 慢查询、日志同步疯狂写入导致 %util=100%、CPU 闲但业务卡，是线上最常见隐性瓶颈。
- **NVMe 多队列设备的 `%util` 语义不准**：可能 >100%（因为多队列并行），这时看 `await` 更可靠。脚本兜底用 /proc/diskstats 时同样有此特性。
- 脚本未装 iostat 时用 `/proc/diskstats` 两次采样兜底算 %util/await，精度略低但够用。
- **文件系统容量（df Use%）是另一种"使用率"**：与 %util（IO 繁忙度）不同，它衡量存储空间占用。根文件系统 ≥95% 临近 ENOSPC，写盘变慢甚至失败。**构建/打包/安装对临时空间敏感，磁盘 %util/await 正常却"慢"时，第一个该查 df**。脚本对本地 fs 用 statvfs 查容量，对网络/FUSE 挂载用短超时探测。

### S 饱和度

看 `await`（IO 请求平均等待时间，ms）与 `avgqu-sz`（队列深度）。

解读：
- **`await` 高 = IO 请求排队拥堵**。HDD >20ms 偏慢，>50ms 严重；SSD/NVMe 应 <5ms，>20ms 就异常。
- **低使用率高饱和度**：`%util` 不高但 `await` 高，是小 IO 多、队列拥堵的典型隐性瓶颈，只看 %util 发现不了。
- `avgqu-sz` 持续 >1 说明请求堆积。

### E 错误率

```bash
dmesg | grep -iE "i/o error|read error|write error|ext4-fs error|xfs.*error"
dmesg | grep -iE "hung_task|reset device|ata.*error"   # 硬件/超时
timeout 3 df -h /data/some-netfs-mount   # 网络/FUSE 挂载用 timeout 探测，卡死会超时
findmnt                                  # 查看挂载树；cat /proc/mounts 列全部挂载点
```

故障案例：磁盘坏道时出现间歇性 IO 卡顿、请求超时，资源使用率不高但业务频繁报错。脚本把 I/O error/文件系统错误/hung_task 等归到磁盘错误率。

**挂载卡死**：网络/FUSE/NFS 挂载断开或对端不可达时，`df`/`stat`/`ls` 该路径会**卡死在 D 状态**（连 `timeout` 的 SIGKILL 都杀不掉，靠超时退出会留 D 状态孤儿进程）。表现是"没报错没高负载但偶发卡顿/命令变慢"--任何触碰该路径的构建/进程都会阻塞。脚本用 `setsid + 2s 超时` 子进程探测 fuse/nfs/cifs 挂载，卡死的标 `unresponsive`，不拖累主脚本。手动排查用 `timeout 3 df -h <mp>`，卡死即超时；修复靠 `umount` 或重启挂载客户端。`Transport endpoint is not connected` / `Stale file handle` / `Remote I/O error` 都是此类故障的典型报错。

> 注意：文章提到的 `fsck -n /dev/sdaX` 可查坏道，但**脚本不自动执行**（对挂载的文件系统有风险且需 root）。仅在解读里建议用户离线手动执行，不替用户跑。

## 网络资源

接口超时、请求失败、分布式调用异常，优先查网络。

### U 使用率

```bash
sar -n DEV 1 5          # 每网卡 rxkB/s、txkB/s、%ifutil
iftop -i eth0           # 实时流量（需安装，交互式，脚本不用）
```

解读：`%ifutil` 是网卡带宽占用。高并发打满带宽会限速。脚本用 sar 的 %ifutil，未装 sar 则跳过此维度（不阻塞其他维度）。

### S 饱和度

```bash
ss -s                   # 连接总数、ESTAB、TIME_WAIT、closed
ss -tan | awk 'NR>1{print $1}' | sort | uniq -c   # 各 TCP 状态连接数
netstat -s | grep -i sync     # 半/全连接队列溢出
ss -tl                          # 监听队列
```

解读：
- **TIME_WAIT 过多**：占满临时端口致新连接建不起来，表现小流量但连接超时。阈值与并发强相关（脚本 warn 5000/crit 20000，需结合业务）。
- **TCP 全连接队列溢出**：Nginx/Java 服务 `somaxconn`/`backlog` 太小，小流量但请求超时。看 `netstat -s | grep -i overflow` 或 `ss -tl` 的 Recv-Q。

### E 错误率

```bash
netstat -s | grep -iE "retrans|reset|fail"     # 重传、复位、失败统计
sar -n EDEV 1 5                                # 丢包、错包
ping -c 100 <target>                           # 连通性与丢包率
```

解读：
- **重传率**：脚本从 `/proc/net/snmp` 两次采样算 `RetransSegs/OutSegs`，>0.5% warn，>2% crit。
- **InErrs 增量 >0**：有错包，提示网卡/链路异常。
- **网卡级错误（sar EDEV）**：脚本现在也跑 `sar -n EDEV`，看 `rxerr/txerr/rxdrop/txdrop`。这是 NIC 层丢包，比 TCP 重传更早暴露--网卡在 TCP 之下丢的包，TCP 重传率不一定立刻反映。任一非 0 即提示物理链路/网卡问题（双工不匹配、线缆/光模块、ring buffer 不足）。
- **accept 队列溢出**：脚本从 `netstat -s` 读 "listen queue overflowed" 与 "SYNs to LISTEN sockets dropped"。非 0（累积值）提示 `somaxconn`/`backlog` 曾不足，是"小流量但请求超时"的典型成因。检查是否仍在增长可两次采样 `netstat -s | grep -iE 'overflow|SYNs to LISTEN'`。
- 线上高频故障：网络偶尔抖动、接口随机超时，使用率饱和度都正常，**只有错误率异常**，最易被忽略。脚本对此专门标记。

## 60 秒极速排查流程

日常不用逐条细查，按 USE 优先级走 1 分钟快查：

1. **查错误（优先排故障）**：`dmesg | tail`（或 `journalctl -k -n 100`）看内核报错、OOM、硬件异常。
2. **看全局负载**：`uptime` + `vmstat 1 5` 快速确认 CPU、内存整体状态。
3. **细分 CPU**：`mpstat -P ALL 1 3`、`pidstat 1 3` 定位高负载进程与单核。
4. **核查内存 Swap**：`free -m` + `vmstat`（si/so）确认内存是否不足、频繁交换。
5. **排查磁盘 IO**：`iostat -x 1 3` 确认 %util/await 是否打满。
6. **查网络**：`ss -s` + `netstat -s` 排查连接状态与丢包。

这个顺序就是 USE 口诀：先排异常、再查负载、最后定位拥堵。`use_scan.py --scan` 自动按这个流程采全四大资源。

## 常见误区

1. **只看使用率，忽略饱和度**：CPU/磁盘使用率不高但饱和度高（r 长、await 高），依旧卡顿。饱和度是隐性瓶颈的关键。
2. **忽略错误率**：不少问题不是资源不足，是磁盘报错、网络丢包、内核异常。不查 E 永远找不到根因。
3. **单点排查不闭环**：别盯着一个资源死磕。USE 的核心是全覆盖遍历、逐项排除、精准缩小范围。
4. **被 free 误导**：`free` 低不等于内存不足，看 `available`。
5. **绝对值不分核数**：cs、负载必须除以核数。4 核的 5 万 cs 和 192 核的 5 万 cs 完全不同。
6. **把快照当铁案**：饱和度类指标波动大，单次采样告警要多次确认。脚本一次快照，建议 `--count 5` 或多次跑。

## USE vs 其他性能方法

USE 是**系统资源层**的全覆盖排查框架，定位"是哪类资源瓶颈"。定位到资源后，要找具体代码/指令，需配合其他方法：

| 方法 | 层级 | 回答什么 | 与 USE 关系 |
|------|------|---------|------------|
| **USE（本 Skill）** | 系统资源 | 是 CPU/内存/磁盘/网络的哪类瓶颈（U/S/E） | 第一步，定位资源方向 |
| **llvm-mca-analysis** | 指令级静态 | 计算端口饱和、依赖链（不改代码先预估） | USE 说 CPU/计算瓶颈后，MCA 找指令级根因 |
| **perf 火焰图** | 代码级动态 | 哪段代码吃 CPU、cache miss、分支误预测 | USE 说 CPU 饱和后，perf 找热点函数 |
| **perf/SPE** | 内存层级动态 | cache miss、LLC miss、真实 IPC | USE 无瓶颈但实测慢时查内存层级 |
| **numactl / numastat** | NUMA 拓扑 | 内存是否跨 NUMA 节点访问、节点内存倾斜 | USE 不建模内存层级；大核数多节点机器（脚本会提示节点数）的跨节点延迟是隐性瓶颈，需 numactl/perf 查 |
| **iotop / pidstat -d** | 进程级 IO | 哪个进程/文件吃磁盘 IO | USE 说磁盘打满后，定位元凶进程 |
| **strace** | 系统调用级 | 进程在等什么 syscall | 排查单进程卡顿根因 |

典型链路：USE 定位"磁盘 IO 饱和" -> iotop 找到"日志同步进程" -> 确认根因。USE 不替代后续工具，而是决定该用哪个后续工具。
