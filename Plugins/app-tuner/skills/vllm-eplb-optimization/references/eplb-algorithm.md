# EPLB 机制说明

本文只描述 **机制层** 的通用做法：冗余副本怎么分配、放置目标是什么、权重怎么搬、窗口与周期怎么影响行为。
不包含任何具体实现的源码、仓库地址、内部路径或调参变量；不同部署在这些机制上的差异，以现场文档为准。

## 目录

- [1. 四类专家与约束](#1-四类专家与约束)
- [2. 三张映射表](#2-三张映射表)
- [3. 副本分配：给谁加副本](#3-副本分配给谁加副本)
- [4. 跨 rank 放置：副本放哪](#4-跨-rank-放置副本放哪)
- [5. 权重搬运](#5-权重搬运)
- [6. 调度窗口与重排时机](#6-调度窗口与重排时机)
- [7. 参数映射](#7-参数映射)
- [8. 何时会负收益](#8-何时会负收益)

## 1. 四类专家与约束

| 概念 | 含义 |
|---|---|
| logical expert | 模型定义、router 能选到的专家，数量 = `n_routed_experts` |
| redundant / extra expert | 额外复制的专家副本 |
| physical expert | 设备上实际占用的专家槽位，数量 = logical + redundant |
| local expert | 单个 EP rank 实际持有的槽位数 = physical / ep_size |

约束（部署时先确认，否则后续计算都无意义）：

```
physical % ep_size == 0        # 槽位能被各 rank 均分
physical >= logical            # 副本只会增加，不会替换
logical  % ep_size == 0        # 否专家本身就无法均分（EP 配置问题）
```

## 2. 三张映射表

运行时用三张表驱动路由与重排：

```
physical_to_logical_map  [layers, physical]
    # 每个物理槽位放的是哪个 logical expert

logical_to_physical_map  [layers, logical, redundant+1]
    # 每个 logical expert 的全部物理槽位，无效位填 -1

logical_count            [layers, logical]
    # 每个 logical expert 当前的副本数
```

另有每 step 清零的计数张量 `expert_count[layers, local]`，记录本 rank 各物理槽位收到的 token 数；
它是重排的唯一负载输入。

## 3. 副本分配：给谁加副本

目标：让"每副本平均扛多少 token"的最大值尽量低。

逐副本贪心（通常对 layers/steps 向量化，一次算所有层）：

```
score        = tokens / logical_count            # 当前每副本负载
score_added  = tokens / (logical_count + 1)      # 假设给它加一份后的每副本负载
候选         = 把当前 max(score) 的位置替换成 score_added 后的新 max
本轮复制     = 使「Σ_step 候选 max」最小的 logical expert
```

直觉：每轮挑"最挤"的专家扩容，但用替换后的 max 而不是当前 max 打分，避免把副本加给已经缓解的专家。
并列时用微小偏置（`+arange * 1e-4`）打破，保证结果确定。

## 4. 跨 rank 放置：副本放哪

副本数量确定后，要把 physical 个槽位摊到各 rank（每 rank 容量 = physical / ep_size）。

- 每个副本的权重 = 其 logical expert 的历史逐 step token 数 / 副本数；
- 按"每副本总负载"降序处理（高负载先放，减少后续选择空间）；
- 逐副本贪心：尝试放入每个未满 rank，选使
  ```
  Σ_step max_rank(该 rank 放入后的逐 step 累计负载)
  ```
  最小的 rank；并列取最小 rank id（稳定）。

这里的关键不是"平均分摊总量"，而是**压逐 step 的峰值**。因为保留了完整的 step 向量，不同 batch 的
热点可以错开放置——这是它比"按平均负载均分"更有效的原因。

## 5. 权重搬运

重排后要把每个 rank 现持的权重搬到"新映射要求的槽位"。两种常见实现：

**跨节点 P2P**

1. 先做卡内自足拷贝（源和目标都在本 rank），减少通信；
2. 为每个 expert 统计 sender（持有者）与 receiver（需要者）集合；
3. 收发两端用同一配对函数决定"谁发给谁"，保证 P2P 两端一致；
4. 按对端 rank 分桶，每桶一次批量 `isend/irecv`，分层复用接收缓冲；
5. 收尾把缓冲写回本卡槽位。

**单机镜像（NVLink/共享内存域内）**

1. 所有 rank 把自己的权重写入一块彼此可见的镜像缓冲（只放一层，逐层刷新）；
2. 两次 barrier：写前等大家读完上一层，写后等镜像就绪；
3. 卡内拷贝从自己的镜像读，远端专家直接从对端镜像读——零 P2P；
4. 同一 expert 的多个槽位在收尾时从第一个填好的槽位复制。

两种方式都要求"原地修改"，避免重分配显存。

## 6. 调度窗口与重排时机

- **滑动窗口**：每 step 把 `expert_count` 写入窗口（环形），重排时用整窗数据算负载；
- **周期触发**：每 N 步重排一次（N = `step_interval`）；
- **首次提前**：第一次重排常放在 `interval/4` 左右，纠正冷启动阶段的明显倾斜；
- **prefill 差异化**：prefill 步的负载形态与 decode 差异大，部分实现选择在 prefill 期间跳过重排，
  或把重排推迟到紧随其后的第一个 decode 步，避开高延迟敏感期。

## 7. 参数映射

`--eplb-config.*` 与机制的对应关系：

| 参数 | 机制含义 | 取值提示 |
|---|---|---|
| `num_redundant_experts` | 全局副本总数，决定每 rank 多几个槽位 | 必须是 ep_size 的整数倍；比例见 SKILL.md「冗余专家比例怎么定」 |
| `window_size` | 判定"谁热"所用的历史步数 | 太短受单 batch 噪声影响，太长跟不上漂移；常取 500~2000 |
| `step_interval` | 重排周期 | 必须小于压测/观测步数，否则整轮不触发；常取 1000~3000 |
| `log_balancedness` | 每步打印 `avg/max` 负载比 | 只用于观测，开启会有通信开销 |

判定失衡用的 `balancedness = avg_tokens / max_tokens`（每层按 rank 求均值/最大值再跨层求和）：
1.0 表示完全均匀；0.9 表示最忙 rank 比平均多约 11% token。

## 8. 何时会负收益

- **流量本来就均衡**：没有可摊平的热点，只剩副本显存与重排抖动；
- **没有稳定热点**：热点在不同 batch 间来回跳，重排刚完成就过期，反复搬运；
- **窗口/周期与负载不匹配**：窗口过短会追噪声，周期过长会在负载已变后才重排；
- **显存已无余量**：副本挤占 KV cache，长上下文先崩。

因此"开了 EPLB"不等于"优化了"：先用多指标确认失衡、再看显存余量，最后才谈参数。
