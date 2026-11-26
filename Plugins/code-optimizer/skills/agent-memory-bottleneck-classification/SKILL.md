---
name: agent-memory-bottleneck-classification
description: >
  把采集到的证据归成统一的瓶颈模型：bottleneck_class（latency/bandwidth/compute/frontend）、
  binding_constraint（到函数或访存）、证据链原始值与封闭轴，并给出下一步该用哪类杠杆。
  场景入口类触发：瓶颈判定、瓶颈分类、结论怎么写、下一步做什么、误判排查。
  瓶颈建模类触发：判定树、compute 还是访存、证据链、封闭轴。
---

# 瓶颈判定与建模

## 判定树（从证据到类别）

```
客户端墙钟高
  ├─ CPU 未饱和 → 等锁 / 等 IO / 等网络 → latency（同步或往返）
  └─ CPU 饱和
      ├─ Retiring 高 / Core Bound 高   → compute
      ├─ Memory Bound 高 + LLC/DRAM 高 → bandwidth 或 latency（区分流式 vs 随机）
      ├─ Frontend / Bad Spec 高        → frontend（指令足迹、分支）
      └─ 载荷字节数大（字段多/向量大）  → 先做载荷瘦身
```

## 输出字段

| 字段 | 要求 |
|---|---|
| `bottleneck_class` | 四类之一 |
| `binding_constraint` | 到函数或到访存（例如"属性数组解码的临时对象分配"） |
| 证据链 | top-down 占比、缺失率、火焰图占比、拆分绝对值的原始数字 |
| `closed_axes` | 已排除方向 + 证据 |

## 常见误判

| 误判 | 纠正 |
|---|---|
| IPC 下降 = 退化 | 剩余工作变成等待内存，属预期 |
| 服务端补丁收益 3% = 服务端没救 | 收益随并发度与架构变化，x86_64 p5 可以到 −48% |
| 归一化后的 miss 率可以直接跨架构比 | 只比趋势 |
| 优化后 L2/L3 miss 上升 = 改坏了 | 载荷变小导致空间局部性变差，正常迁移 |
| 命中率下降带来的"变快" | 先查正确性（见 `agent-memory-bench-gate`） |

## 交接

- 结论是"载荷/解码/水合" → 对应杠杆 skill
- 结论是"服务端每行必走的解码" → `graph-store-readpath-patch`
- 结论是"单核已满但服务端未饱和" → `agent-memory-read-scaling`
