---
name: benchmark-multirunner-topology
description: >
  mem0/milvus LOCOMO benchmark harness 的多 runner 分片 + NUMA 拓扑：MEM0/RUNNER 互为整数倍
  （RUNNER>MEM0 共享全部 mem0 + user 均衡连续区间切分，MEM0>=RUNNER 独占连续块），给每角色绑 NUMA 节点
  （strict/floating/none），内存节点回退避免绑到无内存节点，把 mock 负载按 LLM/embedding 分流到独立实例。
  适用：单 runner 打不满 mem0/milvus 被测服务需要多 runner 并发、要按 NUMA 节点隔离被测/压测/辅助进程、
  或 mock 上游（LLM/embedding）与压测进程抢占 CPU 需要分流。
  不适用反例：单 runner 已够用（多 runner 复杂度不值）；机器无 NUMA 拓扑或单节点；
  或无需分片（mem0/milvus 负载天然可单实例承载）。
---

# mem0/milvus 多 runner 分片 + NUMA 拓扑（标定案例）

## 何时使用

- 单 runner 打不满被测服务，需要多 runner 并发生成负载。
- 机器 NUMA 多节点、且**某节点 size=0（无内存）**，绑定策略必须回退，否则绑上就内存耗尽。
- mock 上游（LLM/embedding）与压测进程抢 CPU，需要分流到独立实例/节点。

## 核心原则

1. **分片先定对齐关系**：MEM0 与 RUNNER 互为整数倍；RUNNER>MEM0 共享、MEM0>=RUNNER 独占。
2. **每角色绑定 NUMA 节点**：被测、压测、辅助进程各占节点，互不抢占。
3. **绑定前逐节点看内存大小**：size=0 的节点不容绑，用"内存节点回退"落到有内存的节点。
4. **mock 负载分流**：把 LLM 与 embedding 拆到独立 mock 实例（避免单 mock 成瓶颈/干扰）。

## 步骤速览

具体算法、表格与边界条件（⚠️）**只在 `references/multirunner-topology.md` 一处维护**，本节给步骤名与去向：

1. **定分片形态**（共享 vs 独占）——任一方向整数倍即可；独占形态叠加黄金卷先校验建卷/回放轮转规则一致，
   否则停止启用 → references §2。
2. **NUMA 绑定三态 + 内存回退**（strict/floating/none；回退取"有内存 ∩ 父级允许"交集并回读核对）→ references §3。
3. **基准拓扑照抄**（标定案例角色/规格/NUMA 完整表）→ references §1。
4. **三处易错修复**（基名转发 / shard 覆盖计数 / 计数器校验）按根因表改 → references §4。

## 门禁

- [ ] 任一方向（MEM0/RUNNER）整数倍均可落地
- [ ] size=0 节点的绑定已回退到有内存节点（取 has_memory ∩ 父级允许节点交集，非照抄全集）
- [ ] 同节点多容器分配不重叠 CPU 集合，启动后核对生效 cpuset（非只信 --cpus 配额）
- [ ] 内存节点启动后回读 cpuset.mems 核对实际获配节点
- [ ] 每角色 NUMA 三态（strict/floating/none）正确
- [ ] mock LLM/embedding 已分流
- [ ] 共享/独占形态计数器校验正确
- [ ] 独占分片 + 黄金卷组合：建卷/回放轮转规则校验一致后才启用，否则退回共享/单 runner

## 结果（标定案例形态）

多 runner 共享/独占形态跑通，MEM0=1/RUNNER=2 不再 die；分组压测 9 数据点数据池完全一致。
细节见 `references/multirunner-topology.md` §1/§6。

## 参考

- `references/multirunner-topology.md`：共享/独占形态切分算法、NUMA 三态与内存回退、mock 分流、
  三处易错修复（基名转发/计数拼接/端口组合校验）根因。