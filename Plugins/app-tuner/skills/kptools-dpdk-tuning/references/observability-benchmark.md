# M8 性能观测与基准方法学

来源：[官方] dpdk `doc/guides/testpmd_app_ug/`
（https://doc.dpdk.org/guides/testpmd_app_ug/run_app.html，26.11.0-rc2 实测核对，
版本溯源见 UPSTREAM.md）、tools 文档；其余为 [经验] 工具方法。

## 基准原则

1. **固定变量**：同流量模型（包长/流数/速率）、同对端负载发生器、同 BIOS/内核/大页状态，
   只改被测项。
2. **先测基线再调**：每一版基线落档（配置快照 + 数字 + 测试命令原文）。
3. **以 PPS 为主指标，bps 辅助**；64B 小包是压力上限测试标准负载 [经验共识]。
4. **测量主体是业务应用，不绑定 testpmd**：testpmd 只是手头没有业务应用时的自包含演示架。
   telemetry 是 EAL 级能力，任何 DPDK 应用都有（被禁/裁剪时降级链：`--stats-period`
   日志差分 → proc-info → 交互 read fwd，实测可用）；PPS 来源优先级：业务应用自身计数 >
   telemetry > proc-info > 外部打流对端计数。cycles/packet = perf cycles(worker TID) ÷
   同窗 Δpackets；自发自测受 tap/vhost 反压失真（计数冻结），A/B 判定优先外部流量源，
   纯计算口径演示用 net_null PMD [实测]。

## testpmd 官方参数（实测核对，26.11）

### 转发引擎
`--forward-mode=`：io（默认）/ mac / macswap / flowgen / rxonly / txonly / csum /
icmpecho / ieee1588 / tm / noisy / 5tswap / shared-rxq / recycle_mbufs [官方]。
- 吞吐基准常用 `macswap`（含一次交换，接近真实 L2 处理）或 `io`（最轻，测设备上限）。
- `--port-topology=paired|chained|loop` [官方]。

### 关键运行参数（默认值均为官方实测核对，旧版本以目标环境文档为准）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--rxq/--txq` | 1 | 每端口收发队列数 |
| `--rxd/--txd` | 128 / 512 | 收发描述符数 |
| `--burst` | 32 | 每 burst 包数（0=驱动默认） |
| `--mbcache` | 16 | mbuf 池 per-lcore cache（≤512） |
| `--mbuf-size` | 2048 | mbuf 数据大小 |
| `--max-pkt-len` | 1518 | 最大包长 |
| `--numa` | 开 | NUMA 感知分配 |
| `--mp-alloc` | native | native/anon/xmem/xmemhuge |

### 统计与观测
- `--stats-period N`：非交互模式每 N 秒打统计 [官方]。
- `--display-xstats name1,name2`：周期显示指定 xstats [官方]。
- `--record-core-cycles` / `--record-burst-stats`：每包 CPU cycles 与 burst 分布 [官方]。
- `--latencystats=N` / `--bitrate-stats=N`：指定 lcore 做时延抖动/码率统计 [官方]。

### 参考命令

```bash
# L2 吞吐基线（paired 双口，2 转发核）
dpdk-testpmd -l 2-4 -n 4 -a 0000:xx:00.0 -a 0000:xx:00.1 -- \
  -i --portmask=0x3 --nb-cores=2 --forward-mode=macswap \
  --rxd=1024 --txd=1024 --burst=32 --mbcache=64 --stats-period 5

# 交互内：start / start tx_first / stop / show port stats 0 / show port xstats 0
# 每包开销：start 后 read fwd core cycles stats
```

## 运行时观测工具

### testpmd 运行时命令（交互态）
- `show port stats <id>`：基础收发计数。
- `show port xstats <id>`：驱动扩展统计（每队列计数、imissed、错误等）——**定位丢包层级**：
  imissed=描述符不足；nombuf=mbuf 池空；ierrors=设备错误 [官方 rte_eth_stats 语义]。
- `show fwd-stats` 类命令看核间分布（以目标环境 help 输出为准）。

### dpdk-proc-info（secondary 查询运行中进程）
- 支持 port stats/xstats、mempool 信息等；**具体选项以目标环境
  [proc_info 文档](https://doc.dpdk.org/guides/tools/proc_info.html) 或
  `dpdk-proc-info --help` 为准**。
- 被查进程需允许 secondary（非 --no-shconf / 非 in-memory-only 配置）[经验]。

### telemetry
- `dpdk-telemetry.py`（EAL 默认开启 telemetry）[官方 EAL]。
- socket 接口可取 ethdev stats、mempool info 等；端点清单以目标环境
  [telemetry 文档](https://doc.dpdk.org/guides/howto/telemetry.html) 为准。
- [经验] 实测注意（26.11）：
  - v2 socket 是 **SEQPACKET** Unix socket（直连时用 `SOCK_SEQPACKET`，不是
    SOCK_STREAM）；
  - 目标进程带 `--file-prefix <prefix>` 时，socket 在
    `/var/run/dpdk/<prefix>/dpdk_telemetry.v2`，客户端要传同样的
    `--file-prefix`，默认路径 `/var/run/dpdk/rte/` 下找不到；
  - `--stats-period` 输出中的 "Throughput" 段需要 `--bitrate-stats=N` 指定统计
    lcore，否则恒为 0（转发计数本身不受影响）。

## 系统级观测（OS 侧对照）[经验]

```bash
perf top -C <worker_cpu>            # worker 热点函数（是否落在 PMD/业务/意外内核路径）
perf stat -C <worker_cpu> -e task-clock,context-switches,cache-misses,LLC-load-misses -- sleep 5
top -H -p <pid>                     # 线程 CPU 分布
numastat -p <pid>                   # 内存分配是否跨节点
vmstat 1                            # si/so 确认无 swap
grep -E 'NMI|LOC' /proc/interrupts  # 隔离核上定时器/中断是否异常增长
```

热点确认在**业务代码**后，深水区移交 code-optimizer 的 `dpdk-optimization` skill。

## 压测流量发生器 [经验]

- pktgen（DPDK 官方 sample）、TRex（真实协议栈、流表）、MoonGen/自研发包器。
- 要求：对端机器不能是被测机自身瓶颈（CPU/网卡独立）；流量模型记录归档。

## 输出纪律

每次基准测试记录：硬件快照（lscpu/lspci/dmidecode 摘要）、BIOS/内核参数、
完整启动命令、流量模型、结果（PPS/bps/时延分布/每包 cycles）。
没有这些信息的数字不进结论（呼应 SKILL.md 禁止编造数字）。
