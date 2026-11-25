---
name: agent-memory-topdown-analysis
description: >
  对记忆框架客户端做 top-down 微架构分析（L1/L2/L3），判断瓶颈在 Retiring / Core / Memory / Frontend
  哪一侧，并据此决定继续做算法优化还是转向载荷与解码。可单独用于任何 CPU 侧热点定位。
  场景入口类触发：top-down、IPC 低、Memory Bound、客户端 CPU 打满、微架构分析、热点定位。
  瓶颈建模类触发：L1/L2/L3 分级、Core vs Memory、IPC 下降是不是退化。
---

# 客户端 top-down 分析

## 采集入口

| 目标 | 工具 |
|---|---|
| 客户端 top-down | `top-down -L1/-L2/-L3` |
| 客户端火焰图 | `py-spy` / `perf record` / `pprof`（按运行时） |
| 行级归因（AArch64） | SPE 采样，经 `arm-spe-analysis` 解析 |
| 微架构参数查询 | `kunpeng_microarch` |

## 采集

```bash
top-down -L1            # 先看四象限
top-down -L2 -L3        # 再区分 Core / Memory 子项
```

- 必须挂**真身 PID**，不要挂 `taskset` 父壳；
- 采集时机器空闲度 ≥ 98%，否则被他人负载污染；
- 采 profile 与纯计时分开跑（同时开会把负载撑大）。

## 经验形态

客户端优化到位后，常见 `IPC 从 ~2.6 掉到 ~1.5`、`Memory Bound 从 ~24% 升到 40~47%`。
**这不是退化**：剩余工作更像"等待内存 + 少而分散的指令"。此时继续做算法级优化收益变小，
该转向载荷瘦身与解码下沉。

## 判读

| 形态 | 含义 | 下一步 |
|---|---|---|
| Retiring 高、Core Bound 高 | 真在算 | 算法/向量化 |
| Memory Bound 高、L3/DRAM 归因高 | 访存受限 | 载荷瘦身、布局、`memory-access-optimization` |
| Frontend Bound / Bad Spec 高 | 指令侧/分支 | 解码下沉、减少分派 |
| IPC 高但吞吐低 | 可能在等锁/IO | 查同步与系统调用 |

## 输出

- 四象限占比原始值（优化前/后）
- `bottleneck_class`：latency / bandwidth / compute / frontend
- `binding_constraint`：到函数或访存

## 注意

`instructions` 计数对抢占与频率漂移免疫，适合跨轮核对；IPC 只在干净的单线程采样里可信。
