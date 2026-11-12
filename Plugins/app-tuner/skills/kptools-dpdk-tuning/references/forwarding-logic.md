# M6 转发框架与应用配置调优

面向应用配置层（worker 模型、burst、批处理形态、热路径纪律检查）。引用：l3fwd 等
sample app 文档（https://doc.dpdk.org/guides/sample_app_ug/）、prog_guide。
涉及**修改源码**的深水区由 code-optimizer 的 `dpdk-optimization` skill 承接；
本模块负责形态选择与纪律检查。

## Worker 模型选择

| 模型 | 形态 | 适用 | 代价 |
|------|------|------|------|
| run-to-completion | 单核完成 RX→业务→TX | 队列少、业务轻（l3fwd 默认形态 [官方 sample]） | 无跨核传递开销 |
| pipeline | RX 核→ring→处理核→TX 核 | 业务重、阶段化 | ring/核间传递、串行化风险 |

- 判据 [经验]：单核 cycles/pkt 接近或超过线速预算（如 64B@10G ≈ 67.2ns/包）时，
  run-to-completion 会先顶不住，考虑 pipeline 或分流。
- 量化手段：testpmd `--record-core-cycles`（cycles/packet）+ `--record-burst-stats`
  [官方]；应用内用 `rte_rdtsc()` 打点对照。

## 核-队列亲和与数据局部性

- 每条 RX 队列归属唯一 worker；worker 与队列、网卡、mempool 同 socket（M4 三点一线）。
- 避免多 worker 消费同一条队列（需显式共享语义支持）。
- RSS/flow 保证会话粘滞（同一流同队列），否则状态表跨核访问/加锁。

## 批处理与预取 [官方 sample 模式]

- `rte_eth_rx_burst` 一次取 ≥8~32 包，循环内避免逐包调用重函数。
- l3fwd 模式：先行 prefetch 下一批 mbuf 与查表地址，掩盖内存延迟 [官方 sample 代码]。
- **经验**：burst 越大 cache 足迹越大；64B 小包吞吐场景 32 是常用起点，实测取舍。

## 热路径纪律检查表（审应用代码，只标记不改码）

| 检查项 | 为什么有害 | 建议方向 |
|--------|-----------|---------|
| printf/RTE_LOG 在每包路径 | IO 与锁竞争，微秒级抖动 | 计数器聚合，错误路径才打日志 |
| 动态分配（malloc） | 走内核/堆锁 | 启动期预分配 + per-lcore 资源池 |
| 共享数据无锁化不足 | false sharing / 缓存行弹跳 | per-lcore 计数器；确需共享用 rte_ring SP/SC |
| 每包获取时间/系统调用 | vDSO 也要看频率 | 定时任务批处理（rte_timer / 帧计数触发） |
| 数据结构跨缓存行 | 一行内容两核改 | cache line 对齐（`RTE_CACHE_LINE_SIZE`，别硬编码 64） |

命中且需要改代码修复 → 移交 code-optimizer 的 `dpdk-optimization` skill，
并在诊断报告中记录移交项清单。

## 查表与算法（配置视角）

- LPM：tbl24/tbl8 结构（prog_guide 官方）；表大 → TLB/cache 压力，规则少时考虑小表。
- ACL / hash：按规则规模选型；hash 表 key 热点（全部命中同 bucket）先查业务流量特征。
- **经验**：查表 miss 率与 cache miss 用
  `perf stat -C <core> -e LLC-load-misses,LLC-misses` 定位——先测再动结构。

## 时延特化（与 M7 呼应）

- 减少逐包平均成本不是降时延的全部；尾部时延主要来自：日志、内存分配、锁、
  定时器集中触发、以及内核抢占（回 M2 核隔离）。
- 时延测量：`--latencystats=N`（testpmd 官方，指定 lcore 做时延/抖动统计）。

## 自检问题清单（审应用代码时逐条问）

1. RX/TX 循环里有没有内存分配、锁、日志？
2. 每包经过的函数有没有重复解析（同一 mbuf 解析两次 header）？
3. 统计是不是 per-lcore，汇总是不是低频异步？
4. burst 之间的空转（无包时全速轮询）是否需要降功耗（→M7）？
5. 结构体是否 `__rte_cache_aligned`？per-lcore 数组是否按行对齐避免 false sharing？
