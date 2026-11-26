# 多 runner 分片 + NUMA 拓扑全量（标定案例）

## 1. 目标拓扑（分组压测，3 组 × 3 数据点）

| 角色 | 实例 | 规格 | NUMA |
|---|---|---|---|
| 被测服务 | 1 | 2u4G | strict 节点 0 |
| 向量库 | 1 | 2u4G | strict 节点 1 |
| mock（LLM+embedding 分流） | 2 | 2u2G | floating 节点 3 |
| runner | 2 | 2u2G | floating 节点 3 |

`MEM0_WORKERS_PER_INSTANCE=2`；被测服务与向量库各 strict 绑不同节点，mock/runner floating 到无负载节点，
LLM 与 embedding 拆成两个 mock 实例分流。

## 2. 分片形态切分

- MEM0 与 RUNNER 互为整数倍（任一方向）。
- **共享形态（RUNNER>MEM0）**：全 runner 共享全部 mem0 + user 均衡连续区间切分（上游协调器语义）；
  修复"MEM0 必须是 RUNNER 整数倍"的 die（如 MEM0=1/RUNNER=2）。
- **独占形态（MEM0>=RUNNER）**：每 runner 独占连续块，零改动。
- **已知边界（停止条件）**：独占形态（MEM0>=RUNNER>1）叠加黄金卷时，golden 全列表轮转假设
  （`u % MEM0_INSTANCES`）与片内轮转归属不一致——建卷写进某实例、回放却按片内查到另一实例，
  同一用户可能被路由到没有其数据的地方。选择该组合前先校验建卷/回放两端的轮转规则一致：
  不一致则**停止启用**，改共享形态（RUNNER>MEM0）或单 runner（这两者规则全一致），并在 contracts 层约束。

## 3. NUMA 三态 + 内存节点回退

- `strict`：CPU + cpuset-mems 同节点。
- `floating`：只绑 CPU。
- `none`：不绑。
- **内存节点回退**：机器 NUMA 4 节点里 node2/node3 size=0（无内存），仅 CPU 节点时 `cpuset-mems` 需要回退——
  这是"逐节点看内存大小"纪律的直接落地，绑错就内存耗尽。**回退值不能照抄 `has_memory` 全集**：
  `has_memory` 是整机内存节点，若父级 cgroup/cpuset 只允许部分节点，cgroup v1 下把 cpuset-mems 设到
  父级不允许的节点会配置失败。取**有内存节点 ∩ 父级允许内存节点**的交集，启动后回读 `cpuset.mems` /
  `docker inspect` 的 `CpusetMems` 核对实际获配节点。
- **同一节点多容器分不重叠 CPU 集合**：floating 到节点 C 的 mock×2 + runner×2（4 个 2u 容器）若都沿用
  节点完整 CPU 列表，`--cpus=2` 只是软配额，四者可被调度到同一批核争用。隔离压测流量时给各容器分配
  non-overlapping core 段（或分到不同节点），启动后核对生效 cpuset。

## 4. 三处易错修复（实跑五连修中的三处）

| 修复 | 根因 | 修法 |
|---|---|---|
| bootstrap 基名转发 | runner 容器内拿容器 ID 当基名，漏转发则下游拿错容器 | 转发 `MEM0_LOCOMO_RUNNER_CONTAINER_ID` |
| shard 覆盖计数 | `+=` 字符串拼接成"1"+"1"→"11" | 改算术加法 |
| 计数器校验 | 固定端口不兼容 mock 多实例分流 | 改按被测服务实际引用的端口组合校验 |

## 5. 为什么 mock 要分流

LLM 与 embedding 是离线回放（mock），但若单实例承载两类回放，会成为并发瓶颈或与被测/压测抢 CPU。
分流到 2 个 mock 实例（2u2G floating 到无负载节点），保证 mock 全程 ~1% CPU、非瓶颈。

## 6. 与其它 skill 衔接

> 下列兄弟 skill 均独立 PR 待合入，合入后按名引用；合入前本节仅作能力占位，不构成可加载依赖。

- 拓扑定好后，相位隔离（`benchmark-phase-isolation`，独立 PR 待合入）以**去中心化全局 barrier** 切
  ingestion/索引就绪/qa——每 runner 的 qa 须等**全部**分片 ingestion 完成 + 全部目标索引就绪才放行
  （跨 runner 全局、phase_windows 全轮统一边界），不是各 runner 相位独立、无需跨片等待。
- 黄金卷（`benchmark-golden-volume-replay`，独立 PR 待合入）的 build/clone 在共享/独占形态上有不同归属，需 contracts 约束。
- 统计设计（`benchmark-statistical-design`，独立 PR 待合入）在多 runner 拓扑上做组间/组内 CV 分解。