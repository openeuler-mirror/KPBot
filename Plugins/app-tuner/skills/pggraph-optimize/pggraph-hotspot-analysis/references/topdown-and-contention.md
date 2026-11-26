# Topdown、缓存争用与客户端 GIL：冲突根因的判别

## 1. 为什么需要这一页

源资料里对“96 并发、NUMA 96 核、CPU 利用率约 40%”给出了两种不同结论：

- **A：服务端缓存/私有内存争用**。每 backend 私有内存约 266MB（反向 CSR + FilterIndex），
  42 个活跃 backend ≈ 11.2GB，超过 96 核共享 L3 约 546MB；LLC miss 9.6%→49.3%，
  backend stall 4%→39.8%，IPC 2.91→1.70，吞吐在 64 并发见顶。
- **B：客户端 Python GIL**。客户端只占约 1 核 CPU，平均只有约 40/96 backend 处于 active，
  其余 `ClientRead`；吞吐平台约 265 q/s，用 Little's Law 反推 GIL 等待可解释；
  另一组 PMU 数据显示 L3 miss stall/DRAM 访问为 0，排除缓存/DRAM。

这两组数据来自不同时间/配置/测量方法，**不能同时当作同一实验的结论**。正确做法是
在目标环境用判别实验决定，而不是选一个故事。

## 2. 判别实验（按顺序）

### 2.1 先排除客户端

```bash
# 客户端 CPU 是否接近 1 核
ps -p <CLIENT_PID> -o pid,pcpu,psr,args
cat /proc/<CLIENT_PID>/stat | awk '{print "utime+stime=" $14+$15}'

# 服务端 backend 状态
psql -At -c "
SELECT state, wait_event_type, wait_event, count(*)
FROM pg_stat_activity
WHERE datname=current_database()
GROUP BY 1,2,3 ORDER BY 4 DESC;"
```

判定：

- 客户端 ~1 核 + 大量 `ClientRead` + 并发增加但 QPS 平台 → 先按客户端瓶颈处理。
- 用 `--mp`/异步/多进程重跑；QPS 明显上升 → 确认 GIL/提交速率。
- 客户端 CPU 不高、backend 大多 active、无锁等待 → 进入服务端。

### 2.2 再看服务端并行度与缓存

```bash
# 每核利用率（只看绑定核）
mpstat -P "${CPULIST}" 1 10

# 单 backend / 核组 PMU
perf stat -p "${BACKEND_PID}" -e cycles,instructions,cache-references,cache-misses -- sleep 10
perf stat -C "${CPULIST}" -e cycles,instructions,cache-references,cache-misses -- sleep 10

# 私有内存/共享 mmap
grep -E '^(Pss|Private_Clean|Private_Dirty|Rss):' /proc/${BACKEND_PID}/smaps_rollup
grep -A6 -E 'pggraph|\[heap\]|anon' /proc/${BACKEND_PID}/smaps | head -80
```

判定：

- backend 大多 active、IPC 明显下降、LLC miss/后端 stall 上升、私有内存随并发叠加
  → 缓存/内存争用成立（A）。
- L3 miss stall/DRAM 访问接近 0、每核利用率与 active backend 数一致、延迟增长主要是
  排队 → 不是缓存/DRAM，回到客户端/连接/调度（B 或 PG 排队）。

### 2.3 容量扫描

固定数据/查询/轮次，C=1,2,4,8,16,32,48,64,96：

- 记录 QPS、每查询 avg、TOTAL、客户端 CPU、服务端每核 CPU、每 backend PSS/Private。
- 若 QPS 在 C=64 见顶、96 下降，且延迟继续上升 → 服务端资源饱和或客户端提交饱和；
  用 2.1/2.2 区分。
- 若 QPS 在低 C 就平台，且 min 延迟不随 C 增加 → 客户端/协议串行（GIL）更可能。
- 注意 SMT：96 逻辑核可能是 48 物理核；用 48 个 primary 线程复测。

### 2.4 SMT/NUMA 对照

```bash
# 只用 primary 线程（示例，按实际拓扑替换）
numactl -C <PRIMARY_CPU_LIST> -m <NUMA_NODE> ...

# 跨 NUMA 对照
numactl -C "${CPULIST}" -m <NODE_A> ...
numactl -C "${CPULIST}" -m <NODE_B> ...
```

- SMT 会让 IPC 下降，但不解释“大量 backend ClientRead”。
- 跨 NUMA 会升高远端访问/延迟；用 `numastat -p` 确认内存是否本地。

## 3. Topdown 指标怎么看

**采集前提**：上表的 Frontend/Backend stall 等 Topdown 桶需要平台的 Topdown 指标组支持
（鲲鹏 devkit topdown、perf 的 tma 指标组等）。采集前先用 `perf list | grep -iE 'topdown|tma'`
（或 devkit 对应命令）确认本机支持的指标组，再按确认的命令与预期输出采集。仅有通用事件
（cycles/instructions/cache-references/cache-misses/branch-misses）时只能算 IPC 与
miss 率，**不得**推算 Frontend/Backend stall 占比——按实际可得的指标报告，本机不支持的
桶明确写"不支持，未采集"。

| 指标 | 含义 | 图负载中的可能原因 |
|------|------|--------------------|
| IPC | 每 cycle 指令数 | 下降 = stall/争用；高但吞吐不涨 = 工作量/算法 |
| Frontend stall | 取指/分支 | 大代码/间接跳转；通常不是图遍历主因 |
| Backend stall | 等内存/执行单元 | CSR 随机访问、cache miss、NUMA、SMT |
| L1/L2/L3 miss | 缓存层级缺失 | 工作集/随机访问；注意 L3 是 tag directory 的架构差异 |
| DRAM local/remote | 本地/远端内存访问 | 工作集超出缓存或跨 NUMA |
| TLB walk | 页表遍历 | 大 mmap/随机访问；也可能正常 |
| context switch/migration | 调度 | 连接/线程过多、绑核不当 |

不要只看“CPU 利用率”：非 idle 也可能是 stall。结合 IPC、stall、每核 active backend 数、
客户端 CPU 一起判断。

## 4. 两种结论的证据对照

| 证据 | 支持 A（缓存/私有内存） | 支持 B（客户端 GIL） |
|------|--------------------------|----------------------|
| 客户端 CPU | 可能不高 | ~1 核 |
| backend 状态 | 大多 active | 大量 ClientRead |
| QPS 随 C | 到峰值后不增/下降 | 低 C 就平台 |
| min 延迟 | 可能随 C 变差（缓存） | 基本不变（服务端执行不变） |
| IPC | 明显下降 | 可能正常 |
| LLC miss/后端 stall | 上升 | 不一定 |
| 私有内存 | 随 C 叠加，超 L3 | 不是主因 |
| `--mp` 复测 | 提升有限 | 明显提升 |
| 降并发/连接池 | 提升效率 | 可能降低排队但 QPS 平台不变 |

## 5. 优化含义

若 A 成立：

- 降低活跃并发/连接池，避免私有工作集超过 L3/CCL。
- 减少每 backend 私有内存：反向 CSR 持久化并 mmap 共享、FilterIndex 直接 mmap、
  受控跳过 CRC（有完整性风险）。
- 绑定 NUMA/CCL，避免跨节点；用 primary 线程避免 SMT 争用。
- 这些是候选，需 A/B 验证；源资料中的“私有 266MB→<5MB、LLC 49%→<15%”是预期，
  不是已实现结果。

若 B 成立：

- 客户端改多进程/异步/原生；`--mp` 是最低成本的验证。
- 连接池限制并发到峰值附近；不要用 `max_connections` 全开。
- 服务端图算法优化可能仍有效，但不会突破客户端提交上限。

若两者同时存在：

- 先解决客户端（成本低、可快速验证），再处理服务端私有内存/缓存。
- 报告分别给出“客户端修复后”和“服务端修复后”的收益，不要混在一起。

## 6. 报告写法

```
场景：C=96，LDBC SF1，csr_readonly，numactl -C ... -m ...
观测：QPS=..., 客户端 CPU=..., active backend=..., ClientRead=...,
      IPC=..., LLC miss=..., backend stall=..., 每 backend PSS/Private=...
判别：客户端 GIL / 服务端缓存 / 两者叠加 / 证据不足
反证：排除了锁、IO、NUMA、TLB、SMT...
限制：数据集口径、采样窗口、PMU 事件可用性
```

证据不足时明确写“不足以定论”，并给出下一步实验；不要用单一指标下结论。
