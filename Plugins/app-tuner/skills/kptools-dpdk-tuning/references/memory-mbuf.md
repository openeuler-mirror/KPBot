# M4 内存与 mbuf 管理

来源约定同 SKILL.md。核心引用：dpdk `doc/guides/prog_guide/mempool_lib.rst`、
`mbuf_lib.rst`、testpmd 手册（https://doc.dpdk.org/guides/prog_guide/mempool_lib.html 等，
版本溯源见 UPSTREAM.md）。

## 原则：NUMA 三点一线 [官方+经验]

**worker 核 socket == 网卡 numa_node == mempool socket_id**。三者跨 socket 是最常见
的隐性性能杀手（跨 NUMA 访存延迟约 1.5~2 倍）。

```bash
# 核对三项
cat /sys/bus/pci/devices/0000:<addr>/numa_node     # 网卡
usertools/cpu_layout.py                             # 核所在 socket
# 应用代码/日志中 mempool 创建的 socket_id
```

testpmd 中对应参数 [官方]：
- `--numa`（默认开启）按端口 NUMA 分配 ring/mbuf；`--no-numa` 反之。
- `--port-numa-config=(port,socket)`、`--ring-numa-config=(port,flag,socket)` 显式指定。
- `--socket-num=N` 全部内存取自指定 socket。

## mempool 调优

| 项 | 建议 | 依据 |
|----|------|------|
| 每 RX 队列所在 socket 一个池 | 必须；禁止全系统共享单 socket 池 | NUMA 原则 |
| per-lcore cache | 适当调大（testpmd `--mbcache` 默认 16，上限 512 [官方]）；命中率可用 procinfo/telemetry 观测 | 官方 cache 机制（mempool_lib） |
| 池大小 | 峰值在途 mbuf × 安全系数；不足表现为 `rx nombuf` 计数增长 | 经验，验证见 M8 |
| mbuf 数据大小 | `--mbuf-size` 默认 2048 [官方]；jumbo 场景按 `max-pkt-len` 配置，避免按最大帧盲目翻倍 | 官方默认值 |

### mbuf 组成 [官方 mbuf_lib]
- 每个 mbuf 头部 + `RTE_PKTMBUF_HEADROOM`（默认 128B，以目标环境版本文档/头文件为准）+ 数据区。
- **经验**：hot path 若频繁 `rte_pktmbuf_prepend`，headroom 余量要核对。

## 大页与 TLB [官方 sys_reqs]

- 大页减少 TLB miss 是官方给出的使用大页的理由；1G 页进一步降低页表压力（64 位应用官方推荐 1G）。
- 判据（页面大小选择）：池总量 GB 级以上优先 1G 页；先确认 CPU 支持（x86 flags 含
  `pdpe1gb`，aarch64 查 CPU features）。

## 进阶路径

| 手段 | 说明 | 出处 |
|------|------|------|
| buffer split / 多池 | `--mbuf-size=N1,N2` + `--rxpkts` 按段收包到不同池（header/payload 分离） | testpmd 官方 |
| indirect mbuf | 原地引用 payload，省拷贝；牺牲重排序/释放复杂度 | mbuf_lib [官方机制] |
| external memory / `--mp-alloc=xmem|xmemhuge` | testpmd 支持外部内存池 [官方] | testpmd 手册 |
| `--mbuf-pool-ops-name` | 换 mempool 驱动实现（如 ring 变体） | EAL 官方 |
| IOVA 连续性 | mempool 优先 IOVA 连续分配；`--no-iova-contig` 允许非连续（配合 mp-alloc=anon）[官方] | testpmd |

## ring（rte_ring）

- 尺寸必须 2 的幂（官方 ring 库约束）。
- 生产者/消费者拓扑固定时用 SP/SC 环，避免无谓的 MPMC CAS 开销 [官方机制+经验]。
- 环空/环满的忙等消耗 CPU：满说明下游慢，先查下游再调环。

## 常见错误（与 M10 呼应）

- 一个大池创建在 socket0，socket1 网卡的队列引用它 → 跨 NUMA。
- cache=0（禁缓存）+ 大量小 alloc/free → mempool 锁竞争（多核抢 ring）。
- mbuf 泄漏（只 tx 不释放）→ 表现为运行一段时间后 `nombuf` 丢包，池大小怎么加都没用。
