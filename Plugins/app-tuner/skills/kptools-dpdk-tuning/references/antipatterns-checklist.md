# M10 常见反模式与坑速查表

现象 → 根因 → 验证 → 处理。标 [官方] 的有文档依据；其余 [经验]。
诊断模式先过这张表再进排查树（workflow-diagnose.md），命中率高。

## 启动/内存类

| 现象 | 根因 | 验证 | 处理 |
|------|------|------|------|
| EAL init 报大页不足 | 预留不够或被其他进程占用 | `grep Huge /proc/meminfo`（Free 数） | 增大预留/清占用方；多进程核对 file-prefix |
| 启动慢到超时 | 运行时预留 1G 页失败回退/内存碎片 | dmesg + EAL 日志 | 启动参数预留大页 [官方] |
| secondary 起不来 | 地址冲突/配置不一致 | 两端日志对比 | `--base-virtaddr`；同版本同大页配置 [官方] |
| hugetlbfs 找不到挂载点 | 未挂载 | `grep hugetlbfs /proc/mounts` | mount + fstab [官方] |

## 收包丢包类

| 现象 | 根因 | 验证 | 处理 |
|------|------|------|------|
| xstats 中 imissed 持续涨 | RX 描述符不足/取包不及时 | `show port xstats`；比对 burst 与核负载 | 加 rxd；worker 别做慢操作 |
| nombuf 涨 | mbuf 池耗尽（泄漏或池小） | mempool usage（procinfo/telemetry） | 排查泄漏：tx 后未 free；池加大的同时查泄漏 |
| ierrors 涨 | 设备级错误/链路问题 | 驱动 xstats 明细 | 按 hns3/驱动文档错误码查 |
| "不丢包但吞吐低" | 流控反压（pause 帧） | 绑定前 `ethtool -a` | 测试场景关流控（两端） |
| 只有大流量才丢 | 描述符/burst 吸收能力不足 | 压测复现 + imissed | rxd/burst 调整（M5） |

## CPU/调度类

| 现象 | 根因 | 验证 | 处理 |
|------|------|------|------|
| PPS 低且 worker 打满 | 热路径开销大（日志/查表/锁） | perf top -C 核对热点 | M6 纪律表逐项过；需改代码移交 dpdk-optimization |
| PPS 低且 worker 不满 | 上游取包慢/队列倾斜 | 每队列收包分布 | RSS/flow 分流检查（M5） |
| 时延毛刺 | 核未隔离/内核抢占/THP | context-switches、/proc/interrupts | M2 核隔离三件套；THP off |
| 偶发卡顿与流量无关 | irqbalance/内核任务飘进 worker | ps -eLo psr | isolcpus+irqaffinity 生效性复查 |
| 多 lcore 绑同一核 | `--lcores` 映射错误 | EAL 启动日志 | 官方警告：ring/mempool/spinlock 劣化甚至死锁 [官方] |

## NUMA/内存类

| 现象 | 根因 | 验证 | 处理 |
|------|------|------|------|
| 数字怎么调都差一截 | worker/网卡/mempool 跨 socket | numa_node + cpu_layout + 池 socket | 三点一线（M4） |
| LLC miss 异常高 | 查表过大/数据结构跨缓存行 | perf stat LLC-load-misses | 表规模、cache line 对齐（M6） |
| `-n` 与实际不符 | 内存通道数猜的 | dmidecode | 按实插通道数 [官方语义] |

## 配置类

| 现象 | 根因 | 验证 | 处理 |
|------|------|------|------|
| 参数改了没效果 | 该驱动不支持该参数（如 pt/ht/wt） | 驱动文档 | 只调文档声明支持的项 |
| vector 路径没走 | offload 挡住/simd 位宽限制 | EAL 日志 PMD 路径报告 | 裁 offload；调高 --force-max-simd-bitwidth |
| 突发流量必丢 | rxd 太小（默认 128） | imissed 与流量突发对齐 | rxd 1024 起试（官方默认值参考 M8） |
| 升级后性能回退 | 参数改名（--socket-mem→--numa-mem）/默认值变化 | 对比两版本文档 | 按 UPSTREAM.md 版本差异核对 |

## 处置纪律

- 每条命中项：记录验证证据，改动一项测一项。
- 同一现象命中多项时，按证据强度排序处理，不并行改多项。
