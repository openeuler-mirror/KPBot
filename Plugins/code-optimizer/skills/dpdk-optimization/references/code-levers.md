# DPDK 热路径代码杠杆库

按瓶颈类路由（来自 bottleneck-modeling.md 的 `next_lever_hint`）。
标记约定：[官方 sample] = l3fwd 等 DPDK 官方示例代码模式；[官方机制] = DPDK 库文档
机制（版本以目标环境为准）；[经验] = 工程经验，需现场 A/B 验证。
每条杠杆含：适用瓶颈 / 做法 / 验证 / 风险。

## §0 热路径纪律表（任何瓶颈类都要先过）

必改项——先修纪律再谈杠杆，否则杠杆收益会被这些开销吸收：

| 禁止 | 根因 | 替代 | 检查方法 |
|------|------|------|---------|
| printf/RTE_LOG 在每包路径 | IO 与锁竞争，微秒级抖动 | per-lcore 计数器聚合，错误路径才打日志 | grep RX/TX 主循环调用链 |
| 动态分配（malloc/free/rte_malloc 每包） | 走内核/堆锁 | 启动期预分配 + per-lcore 资源池 | grep 主循环调用链 + perf 热点 |
| 共享计数器/状态每包原子 RMW | 缓存行弹跳 | per-lcore 计数器 + 低频异步汇总 | objdump 找 `ldaxr/stlxr`（aarch64）/ `lock` 前缀（x86）审计 |
| 每包获取时间/系统调用 | vDSO 也有成本；真 syscall 更贵 | 帧计数触发 / rte_timer 低频批处理 | strace -c 抽样 + 热点函数 |
| 数据结构跨缓存行被多核写 | false sharing | `__rte_cache_aligned` 对齐 + per-lcore 分行 | perf c2c / SPE 行级归因 |

- 对齐用 `RTE_CACHE_LINE_SIZE`（目标平台编译期值），**不要硬编码 64** [官方机制]。
- 每包经过的函数确认无重复解析（同一 mbuf header 解析两次）——一次性审计即可。

## §1 RX/TX 循环结构（compute / pipeline 类首选）

### L1.1 burst 批处理 [官方 sample]
- `rte_eth_rx_burst` 一次取 ≥8~32 包，循环内避免逐包调用重函数（时间获取、
  日志、单包 send）。
- 验证：`--record-burst-stats` 看实际 burst 分布；cycles/pkt 下降。
- 风险：burst 过大 cache 足迹变大；64B 吞吐场景 32 是常用起点，实测取舍 [经验]。

### L1.2 双循环预取（l3fwd 模式）[官方 sample]
- 先行循环只做 `rte_prefetch0`（mbuf + 查表 key 地址），主循环做实际处理，
  掩盖查表/访存延迟：

```c
/* 先行：预取下一批的处理所需数据 */
for (i = 0; i < nb_rx; i++) {
    prefetch mbuf metadata + lookup key；
}
/* 主循环：查表 + 转发 */
for (i = 0; i < nb_rx; i++) {
    lookup/process/tx；
}
```

- 验证：SPE LLC-miss 行级归因变化 + PPS；prefetch 距离/行数微调实测。
- 风险：无收益常见于查表已命中 L1（先看瓶颈模型再上）。

### L1.3 无包空转处理 [经验]
- `nb_rx == 0` 的轮次做 housekeeping（统计聚合、管理面轮询、link 状态检查）
  而非纯烧 CPU；低时延目标保持纯忙轮询（与功耗目标冲突，先确认目标——
  冲突声明见 kptools-dpdk-tuning M7）。
- 验证：空转轮占比（帧计数）；目标指标无回退。

### L1.4 worker 模型切换 [官方 sample 形态]
- run-to-completion（单核 RX→业务→TX）↔ pipeline（RX 核→ring→处理核→TX 核）。
- 触发条件：瓶颈建模确认单核 cycles/pkt 预算超支且业务可阶段化。
- 做法：阶段间 `rte_ring` 传递（SP/SC，见 §4.2）；重分发逻辑按流粘滞。
- 风险：最大的一项改动——核间传递本身有成本（每包 2 次入队出队），
  必须先有预算证据；改变并发语义需用户确认。

## §2 数据布局与局部性（memory-latency 类首选）

### L2.1 per-lcore 化 [官方机制+经验]
- 统计计数、临时缓冲、会话状态分片改为 per-lcore 实例；汇总低频异步
  （rte_timer/帧计数触发）。
- 验证：objdump 确认热路径无共享行写；A/B cycles/pkt。
- 风险：语义变化（全局计数变近似值）需业务确认。

### L2.2 cache line 对齐与分行 [官方机制]
- 共享读多写少结构 `__rte_cache_aligned`；per-lcore 数组每元素独占 cache line
  （元素大小补齐到 RTE_CACHE_LINE_SIZE）。
- 验证：perf c2c 映射消除；SPE 归因不再指向聚合行。

### L2.3 热冷字段分离 [经验]
- 包处理只触碰的热字段（下一跳、队列、标志）聚合进一个 cache line；
  管理面字段（配置、统计、寿命）分离到冷结构。
- 验证：SPE miss 行级归因集中于热结构且 miss 数下降。

### L2.4 AoS→SoA / 元数据数组化 [经验]
- 批量处理场景把 per-packet 元数据拆成并行数组（dst_port[]、flag[]...），
  配合向量化分类（§3.4）。联动 `memory-access-optimization` skill。

### L2.5 查表 entry 布局 [经验]
- 查表结果 entry 压缩到单 cache line（key 与 value 同行）；会话级结果缓存
  复用（业务允许时，首包查表后续包直取）。
- 验证：LLC-load-misses 下降 + PPS。

## §3 查表与解析（compute + memory-latency 类）

### L3.1 重复解析消除 [经验]
- 同一 mbuf 的 header 只解析一次，解析结果（协议类型/偏移/端口）透传给后续阶段，
  禁止每阶段重新读包头。
- 验证：热点函数中 header 读取指令占比下降。

### L3.2 LPM 规模与结构 [官方机制]
- 规则数少（< 数千）时评估 hash 或直接数组索引替代 LPM（tbl24/tbl8 两级
  访存对小表是浪费）；表大时控制 tbl8 碎片。
- 验证：查表段 cycles 占比 + LLC miss。

### L3.3 查表实现选型 [经验]
- rte_hash vs 自研：按 key 长度/命中率/规模选型；key 热点（全部命中同 bucket）
  先查业务流量特征——那是流量问题不是代码问题。
- 联动 `hashmap-optimization` skill 做深水区（探测路径/布局/预取）。

### L3.4 协议类型分支 → 向量化分类 + 热冷分离 [官方 sample]
- l3fwd 思路：按协议/端口分组批量处理，把逐包分支变成批量分类（NEON 比较
  指令选 port），异常/罕见类型走 out-of-line 冷路径。
- 验证：`br_mis_pred` 事件下降；objdump 确认 NEON 分类指令落地。
- 联动 `branch-elimination` / `apply-vectorization` skills。

### L3.5 NEON 批量解析 [经验]
- 批量场景用 NEON 一次解析/校验多个 header 字段（校验和预筛、类型分类、
  地址截取）。aarch64 上注意对齐要求与 NEON/SVE 选择（`kunpeng_microarch`）。
- 验证：/tmp 探针 objdump 确认指令映射；A/B 实测。

## §4 同步与统计（sync-syscall 类首选）

### L4.1 per-lcore 计数器 [官方机制+经验]
- 见 §2.1；热路径禁止共享原子 RMW。多 worker 聚合点只在低频任务里求和。

### L4.2 rte_ring SP/SC [官方机制]
- 生产者/消费者拓扑固定（单 RX 核→单处理核）时用 SP/SC 环，省 MPMC CAS；
  拓扑可能变化时保持 MP/MC 正确性优先。
- 验证：ring 函数段 cycles 占比下降。

### L4.3 锁收敛 [经验]
- 读写锁/自旋锁热点先量化（消融实验：临时跳锁测贡献，正确性降级仅作归因）；
  依次尝试：写意图预取 → SeqLock 无锁读 → 分片（owner 分片）。
  深水区联动 `hashmap-optimization`（锁杠杆完整台账在彼处）。

### L4.4 批量提交点收敛 [经验]
- 多个逐包原子操作合并为每 burst 一次批量提交（局部变量累加，循环出口一次性
  更新共享结构）。
- 验证：原子指令审计计数下降；正确性口径（统计值容差）确认。

## §5 内存管理（bandwidth / memory-latency 类）

### L5.1 mbuf 复用与回收 [官方机制]
- 转发类应用收发同 mbuf（修改 in-place，零重分配）；recycle 思路
  （testpmd recycle_mbufs 引擎 [官方]）TX 完成后直接回 RX 池路径。
- 验证：mempool alloc/free 热点消失；cycles/pkt。

### L5.2 indirect mbuf 零拷贝 [官方机制]
- 大包/分片场景引用原 payload（`rte_pktmbuf_attach`）省拷贝；牺牲释放顺序
  与重排复杂度——语义变化需业务确认。
- 验证：每包 memcpy 字节数归零或收敛；DDR 带宽实测。

### L5.3 拷贝路径审查 [经验]
- 逐包 memcpy 大块（>cache line）是带宽杀手：能引用不拷贝，能 in-place 不搬运。
- 验证：`perf stat` DDR 带宽 + d2d 拷贝热点函数占比。

## §6 指令层（compute 类，最后做）

必须有 PMU 证据支撑，联动既有 skills：

| 杠杆 | 证据门槛 | 联动 skill |
|------|---------|-----------|
| 循环展开 | 热点循环开销占比 + branch 开销 | `loop-unrolling` |
| 软件预取距离调优 | SPE 延迟归因 + 预取未命中窗口 | `prefetch-optimization` |
| 分支消除/likely | `br_mis_pred` 事件占比 | `branch-elimination` |
| NEON/SVE 向量化 | Retiring 段标量指令密集 | `apply-vectorization` |
| 指令选择（位运算/浮点延迟） | `kunpeng_microarch` 延迟表核对 | `kunpeng_microarch` |

- **先微验证再实施**：/tmp 写 5 行探针函数 objdump 确认 intrinsic/builtin → 实际
  指令映射符合预期；实施后核对热点函数反汇编确认指令真的落地，防静默回退 [经验]。

## 负结果台账（空表起始）

| 手段 | 现象/场景 | 结果 | 根因分析 | 启示 |
|------|----------|------|---------|------|
| （示例格式）双循环预取 | 查表全在 L1 命中的小表场景 | PPS 无变化 | 无内存延迟可掩盖 | 预取杠杆前先确认 LLC miss 归因 |

- 记账纪律：无收益即回滚代码；每条含手段/现象/结果/根因/启示；实施前对照本表
  确认待实施项与已证伪项机制不同。

## 杠杆实施通用要求

1. 单变量：每轮只改一个杠杆，`#ifdef`/模板参数隔离，A/B 双构建。
2. 先正确性后性能：正确性口径（阶段 1c）全过才进 A/B。
3. A/B 交错多轮取中位，读数筛选与确定性锚见 SKILL.md 测量纪律。
4. 收益在噪声带内（<2-3%）即回滚并记负结果台账。
5. 每轮采纳后复测瓶颈模型（bottleneck-modeling.md），按新 `next_lever_hint` 继续。
