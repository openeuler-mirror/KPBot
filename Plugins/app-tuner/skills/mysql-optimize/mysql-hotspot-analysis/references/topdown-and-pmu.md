# Topdown 与 PMU

## 1. 什么时候用

- on-CPU 火焰图已定位到函数，但想知道“为什么这个函数慢”：指令多、cache miss、
分支预测失败、前端取指受限。
- 评估向量化/数据结构/内存布局改动的潜在收益。
- 区分“计算密集”和“访存/延迟受限”。

不适用：等待/锁/IO（用 off-CPU 与等待事件）、SQL 语义问题（用 EXPLAIN/慢日志）。

## 2. Topdown 指标

| 层级 | 指标 | 含义 |
| --- | --- | --- |
| L1 | Retiring | 有效指令占比 |
| L1 | Bad Speculation | 分支预测失败/清空 |
| L1 | Front-End Bound | 取指/解码受限 |
| L1 | Back-End Bound | 执行/访存受限 |
| L2 | Memory Bound | L1/L2/L3/DRAM 缺失 |
| L2 | Core Bound | 执行端口/依赖链 |

判读：

- Back-End + Memory Bound 高：看 cache miss、访存模式、数据结构大小。
- Front-End Bound 高：代码体积/分支/i-cache；LTO/PGO 可能相关。
- Bad Speculation 高：分支不可预测；数据结构/算法可能相关。
- Retiring 高：指令效率问题（做太多工作/重复计算）。

## 3. 采集方式

```
# perf stat（需 PMU 权限）
perf stat -p "${PID}" -- sleep 10

# Topdown（若 perf 支持）
perf stat --topdown -p "${PID}" -- sleep 10

# 其他工具（按环境）
# - devkit / 厂商性能工具
# - Intel: pcm / pmu-tools（toplev）
# - AMD: uprof
```

- 容器/虚机可能禁用 PMU（`perf_event_paranoid`、hypervisor 限制）。
- 上述命令的 PID 必须是按所选连接唯一映射的实例（见 `profiling-playbook.md` §1）；
  多实例未锁定时不要执行，避免把不同实例的计数混在一起。
- 多实例/多线程时，按进程统计是汇总值；需要按线程用 `-t` 加线程号。
- 采样窗口覆盖稳态；避免把启动/预热计入。

## 4. 与 MySQL 的结合

| 现象 | 可能 Topdown 特征 | 方向 |
| --- | --- | --- |
| B+Tree 比较慢 | Back-End/Memory Bound、分支失败 | 减少比较/offset、fast-tuple、数据结构 |
| 排序键生成慢 | Core Bound、指令多 | NEON/批量 ASCII、减少解码 |
| Buffer Pool 命中但慢 | Memory Bound/cache miss | 页布局、预取、减少随机访问 |
| 解析/优化慢 | Front-End Bound、i-cache | 减少代码体积/LTO/PGO |
| 自旋多 | Retiring 高但无效工作 | 降低争用/调整自旋 |

注意：Topdown 不是因果证明；必须回到函数与代码路径。

## 5. 限制与误判

- 虚拟化/容器下计数器可能不可用或不准；先验证 `perf stat` 能出数。
- 频率/温度/邻居负载会改变计数；A/B 要同环境。
- Topdown 的 Memory Bound 不等于“加内存”；可能是访存模式/数据结构。
- 指令数下降不一定变快（可能增加分支/延迟）。
- 不要把 PMU 数字单独写成收益结论；要与端到端 TPS/P95 对齐。

## 6. 输出建议

```
Topdown（采样 15s，mysqld PID，稳态 RO）：
- Retiring / Bad Speculation / Front-End / Back-End 占比（按本轮实测填写，注明采样窗口与来源）
- Back-End 中 Memory Bound 为主，L3 miss 高
- 对应热点：row_search_mvcc / cmp_dtuple_rec / rec_get_offsets
- 推断：比较与访存受限；候选：fast-tuple、减少 offset 重算
- 验证：同口径 A/B + MTR
```
