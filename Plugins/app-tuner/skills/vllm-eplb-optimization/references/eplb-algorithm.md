# EPLB 机制剖析（以 DeepInfer 实现为例）

> 这是**机制参考资料**，不是本技能改写对象的源码。本技能操作的部署是 vLLM 的 P 节点
> 启动模板；这里用 DeepInfer 的 EPLB 实现来解释概念、放置目标与调参依据，两者在"冗余副本 + 周期性
> 重排"的骨架上一致，差异见文末。
>
> 源码版本：`deepseek-ai/DeepInfer`（`main` 分支，git tree SHA 398 entries）
> 涉及文件：
> - `deepinfer/model_executor/eplb/__init__.py`（`ExpertLoadBalancer` / `Manager`，运行时调度与统计）
> - `deepinfer/model_executor/eplb/policy.py`（`rebalance_experts_global` / `_batch_greedy_indices`，放置策略）
> - `deepinfer/model_executor/eplb/shuffle.py`（专家权重量排：P2P 通用版 + 单机 IPC 镜像版）
> - `deepinfer/model_executor/models/v4_modules/moe.py`、`mlp.py`、`deepseek.py`（集成点）
> - `deepinfer/model_executor/model_loader.py`、`deepinfer/environ.py`（冗余专家决议与调参开关）

---

## 0. 源码目录与代码量概览

```
deepinfer/model_executor/eplb/
├── __init__.py   434 行   ExpertLoadBalancer / Manager：运行时状态、计数、调度、统计
├── policy.py     140 行   rebalance_experts_global + _batch_greedy_indices：放置策略
└── shuffle.py    218 行   专家权重的原地重排（跨卡 P2P / 单机 IPC 镜像）
```

EPLB 运行时状态类 `ExpertLoadBalancer`（构造函数关键字段）：

```python
# eplb/__init__.py:42 (ExpertLoadBalancer)
class ExpertLoadBalancer:
    expert_count: torch.Tensor          # [num_moe_layers, num_local_physical_experts] 每 step 清零
    physical_to_logical_map: torch.Tensor  # [num_moe_layers, num_physical_experts]
    logical_to_physical_map: torch.Tensor  # [num_moe_layers, num_logical_experts, num_redundant+1]
    logical_count: torch.Tensor            # [num_moe_layers, num_logical_experts]
    moe_load_balance_ratio: tuple | None   # (avg, max) token 数
```

---

## 1. 核心术语

EPLB 在 DeepInfer 里解决的是一类专门问题：**MoE 各专家负载不均，且冗余副本（redundant expert）可以被灵活复制与搬动**，从而把热专家多放几份、冷专家少放，并把这些副本”摊平“到各 GPU 上。

| 术语 | 含义 | 关系 |
|---|---|---|
| **logical expert** | 模型定义（gate 能选到）的专家，数量 = `n_routed_experts` | 上层路由只面向 logical 编号 |
| **extra / redundant expert** | 额外分配的专家副本 | `physical = logical + extra` |
| **physical expert** | GPU 上实际放置的专家副本，数量 `num_physical_experts` | 必须能被 `ep_size` 整除 |
| **local expert** | 本 rank 实际持有数 | `local = physical // ep_size` |

一致性约束（代码中多处 assert）：
```
num_physical_experts % ep_size == 0
num_physical_experts >= num_logical_experts
```

---

## 2. 三类核心映射（数据结构）

`ExpertLoadBalancer` 维护三张表，驱动路由与重排：

```
physical_to_logical_map  [num_moe_layers, num_physical_experts]
     # 每个物理槽位放的是哪个 logical expert

logical_to_physical_map  [num_moe_layers, num_logical_experts, num_redundant+1]
     # 每个 logical expert 的全部物理槽位（副本列表），无效位 = -1

logical_count            [num_moe_layers, num_logical_experts]
     # 每个 logical expert 当前拥有的副本数
```

另外每 step 清零的计数张量：
```
expert_count  [num_moe_layers, num_local_physical_experts]
     # 本 rank 各物理槽位累计收到的 routing token 数（作为负载来源）
```

---

## 3. 算法流程总览

```
每个 step (sample 后)：
  ExpertLoadBalancerManager.step(is_prompt)
    └─ 每个 ExpertLoadBalancer.step(is_prompt)
         ├─ _update_rearrangement(is_prompt)   # 滑动窗口 + 周期触发重排
         └─ _update_load_balance_stats()        # 周期 all-gather，上报负载均衡度
         └─ expert_count.zero_()                # 计数清零

达到重排周期时：
  _rearrange_experts()
    ├─ _initialize_shuffle()                    # 首次：释放缓存 / 建 symm_mem IPC 镜像
    ├─ 窗口 token 计数聚合 → all_reduce 全局
    ├─ rebalance_experts_global(...)            # 策略：算新 placement
    ├─ shuffle 权重                            # P2P 或 intranode 镜像搬运权重
    └─ 更新三张映射
```

---

## 4. 各算法详解

### 4.1 冗余专家数量决议 — `resolve_num_extra_experts`（eplb/__init__.py:21）

目标：取**不小于 `min_num_extra_experts` 的下限**，且使 `physical = logical + extra` 能被 `ep_size` 整除。

```
padding = -(num_routed_experts + min_num_extra_experts) % ep_size
extra   = min_num_extra_experts + padding
```

> 例：routed=384，min_extra=0，EP=16 → extra=0；若 min_extra 与 EP 不整除则向上取整补满。
> 上游 `model_loader._configure_moe_redundancy` 用它对每个 MoE layout（backbone / DFlash）写 `n_redundant_experts`。

### 4.2 初始放置 — `get_initial_global_physical_to_logical_map`（eplb/__init__.py:31)

物理槽位前 `num_routed_experts` 份放逻辑专家本身，多出的 `extra` 份**循环复制** routed 专家：

```
[0,1,...,num_routed-1] + [i % num_routed for i in range(num_extra)]
```

### 4.3 全局重平衡策略 — `rebalance_experts_global`（policy.py:73)

输入：窗口内各 logical expert 的 token 计数 `[steps, layers, logical]`。
分两阶段：

**(a) 冗余副本贪心分配（给热专家加副本）**

循环 `num_redundant_experts` 轮，每轮复制一个 logical expert：
- `score = tokens / logical_count`：当前每副本负载（用 `+arange*1e-4` 破坏并列，保证可区分）；
- `score_added = tokens / (logical_count+1)`：假设给 expert j 加一份后的每副本负载；
- 对每层取当前 `argmax`（当前最高每副本负载的专家），替换为其 `score_added` 后重算 `max`，**按 step 求和取 argmin** → 本轮复制该专家；
- 把新副本写入下一个空 physical 槽位，`logical_count+1`，更新 `logical_to_physical_map`。

> 本质：每轮挑"每副本负载最高"的专家复制，让 max(每副本负载) 尽量下降——即给最挤的专家扩容。

**(b) 跨 rank 贪心放置 — `_batch_greedy_indices`（policy.py:12)**

确定副本后，把 `physical` 专家摊到各 rank（每 rank 容量 `capacity = local`）。
- 每个 physical 专家权重 = 其 logical 专家的**历史逐 step 负载** 除以副本数（`physical = tokens/logical_count` 的向量）；
- 按每副本总负载降序（`argsort(descending, stable)`）把专家排进一个处理顺序；
- 逐专家贪心：尝试放进每个未满 rank，用以下目标打分并取最小：
  ```
  sum over steps: max over ranks (该专家放入后的 rank 累计负载)
  ```
  即最小化"逐 step 的最大 rank 负载之和"，同分时取最小 rank ID（稳定）；
- 生成 `indices`（每层一个排列，每连续 capacity 个元素 = 一个 rank 的物理专家）。

> 目标函数（注释原文）：`min sum_{step} max_{rank} (该 rank 被分配的逐 step 负载)`——等价于平衡高峰负载，且因为持有的是完整历史窗口向量，天然考虑**跨 batch 互补**（不同 step 高负载专家错开摆放）。

返回 `physical_to_logical_map / logical_to_physical_map / logical_count`。

### 4.4 专家权重量排 — `shuffle.py`

重排目标：新旧两套 `physical→logical` 映射，把某 rank 现持的权重搬到位置上"应该持有"的新专家权重。原地（inplace）修改 `experts_weight`，避免重分配显存。

**通用版 `expert_model_parallel_shuffle_inplace`（NCCL batched P2P）**
- 预先规划：`_plan_intra_copies` 计算卡内能自足的 (src,dst) 拷贝对 + 卡片是否已满足；
- `_remote_routes` 统计每个 expert 的 sender 集合（持有该 expert 的 rank）与 receiver 集合（需要但本卡没有）；
- `_paired_sender`：sender/receiver 用同一函数配对，保证 P2P 两端一致（均分发 + 轮转补齐）；
- 发送：对每个持有 expert，把权重 `isend` 给配对到自己名下的 receivers；接收：对缺失 expert `irecv` 进接收缓冲；
- 按 `(对端 rank 差 mod DEEPINFER_REARRANGE_P2P_GROUPS)` 分桶，每组 `batch_isend_irecv` 一次发完（分层复用同一组接收缓冲）；
- 收尾：缓冲 → 本卡槽位。

**单机版 `expert_model_parallel_shuffle_inplace_intranode`（symm_mem IPC 镜像）**
- 仅当 `PG.is_within_nvl_domain(ep_group) and PLATFORM.is_gpu`（NVL link 内、GPU 平台）才启用；镜像只容纳**一层**权重，逐层刷新复用；
- 每层：`symm_mem.rendezvous` 建立可达各 rank 的镜像缓冲 (`_ipc_weight_buffers`)，barrier 双层同步；
- 卡内拷贝从自己的镜像读（避免覆盖来源）；远端专家直接从对端 rank 的镜像读。**零 P2P 通信**。

### 4.5 调度与时机 — `_update_rearrangement`（eplb/__init__.py:146)

- 滑动窗口：`expert_count` 每 step 拷贝进 `_expert_count_window[window_size, layers, local]`（环形索引）；
- 首次重排**提前**到 `interval/4` 后（纠正冷启动不均衡）；
- `is_prompt` 与 `is_normal_engine` 控制 prefill 时是否跳过重排：
  - `DEEPINFER_REARRANGE_AFTER_PREFILL_IN_NORMAL_ENGINE=true` → prefill 步把 `_rearrangement_step` 推到 `interval-1`，让**下一个非 prefill step 立即重排**（把重排挪到 decode 侧）；
  - 否则 prefill 跳过一次统计。
- 权重重排的副作用（`_initialize_shuffle`）：首次先 `empty_cache()` 防 OOM。

### 4.6 负载均衡度上报 — `_update_load_balance_stats`（eplb/__init__.py:167)

每 `DEEPINFER_MOE_LB_STAT_INTERVAL`（默认 128）步，把各 rank 每层 token 数 `all_gather`，得到 `(avg, max)` → `moe_load_balance_ratio`；经 `ExpertLoadBalancerManager.take_moe_load_balance_ratio` 挂到 `sampler_output.post_funcs`，供用户观测。

重排后日志（rank0）输出 `rank_balance / logical_balance / physical_peak / time` 等指标；`DEEPINFER_SHOW_EPLB_DETAIL=true` 时用 `tabulate` 逐层打印 Max/Avg。

---

## 5. 集成点

| 位置 | 作用 |
|---|---|
| `model_loader.finalize_model_config` | `_configure_moe_redundancy` 决议每 layout 的 `n_redundant_experts` |
| `deepseek.py:344-364` | 按同构 layout 分组建 `ExpertLoadBalancer`，`set_current` 注册单例 Manager |
| `deepseek.py:402-409` | `sample()` 里 `expert_lb.step(is_prompt)`，更新重排与统计 |
| `mlp.py:392,474,555` | 路由 dispatch 用 `logical_to_physical_map/ logical_count` 计算物理槽位；`cumulative_local_expert_recv_stats=expert_count` 做负载计数 |
| `moe.py` | 定义 logical/extra/physical/local 语义与 `ParallelRoutedLinear` 参数规模 |
| `environ.py` | 全部运行调参开关（见下） |

---

## 6. 可调参数（环境变量，environ.py）

| 变量 | 默认 | 含义 |
|---|---|---|
| `DEEPINFER_OVERRIDE_NUM_EXTRA_EXPERTS` | (未设) | 冗余专家下限，被 `resolve_num_extra_experts` 向上取整到可整除 |
| `DEEPINFER_EXPERT_COUNT_WINDOW_SIZE` | 100 | 负载统计滑动窗口长度（按住 ≤ step_interval） |
| `DEEPINFER_EXPERT_REARRANGEMENT_STEP_INTERVAL` | (未设) | 重排周期（step 数）；0 = 关闭重排 |
| `DEEPINFER_REARRANGE_AFTER_PREFILL_IN_NORMAL_ENGINE` | false | normal engine 下把重排挪到 decode 步 |
| `DEEPINFER_MOE_LB_STAT_INTERVAL` | 128 | LB 统计 all-gather 周期 |
| `DEEPINFER_REARRANGE_P2P_GROUPS` | 1 | P2P 分桶组数 |
| `DEEPINFER_SHOW_EPLB_DETAIL` | false | 重排后逐层打印 hot 布局 |
| `DEEPINFER_FORCE_LOAD_BALANCE` / `FORCE_IMBALANCE_RATIO` | — | 强制负载/失衡（测试用） |

---

## 7. 设计要点小结（值得关注的工程选择）

1. **两层贪心分离**：先决定"复制谁"（冗余副本扩容），再决定"放哪卡"（rank 放置），避免一次性混合优化的高复杂度。
2. **面向高峰/跨 batch 的放置目标**：rank 放置不是简单按平均负载均分，而是最小化"逐 step 的 max-rank 负载和"，会利用批次间互补性错开热点。
3. **原地权重搬运 + 分层复用缓冲**：显存友好；单机走 symm_mem/IPC 镜像实现零通信重排（NVL 内），跨机走分桶 batched P2P 降低通信次数。
4. **滑动窗口 + 提前首次重排 + prefill/decode 差异化调度**：兼顾冷启动不均衡与在线漂移，并可避开 prefill 的高延迟敏感期。
5. **纯 GPU 张量实现贪心**（`scatter_add`/`argsort`/`masked_fill + argmin`），策略计算本身可向量化、可并行，而非逐层串行 Python 循环（仅外层 job 循环）。

---

## 附：与 STAIR 类实现的主要差异（对照参考）

DeepInfer 该实现明显 **更偏"全量全局重排"**，而 STAIR 类实现是 **"增量微迁移 + 阈值门控（stair 阶梯）"**：
- DeepInfer 每次重排都在**全局窗口数据**上重算完整物理布局并搬运权重；STAIR 类实现用 `window_size / absolute_balance_threshold` 等门控只在失衡超阈值时才触发，并支持 `rank_transfer_limit` 限制单次迁移量。
- DeepInfer 的触发是**固定 step 周期**（`REARRANGEMENT_STEP_INTERVAL`），无显式的"失衡阈值/防抖"；STAIR 类实现是**按 balancedness 阈值**触发。
- DeepInfer 的"冗余副本"供给能力与 `logical_count` 显式建模在映射中；STAIR 类实现靠 `num_redundant_experts` + `logical_to_physical` 张量，机制接近但量化门控与迁移预算不同。

> 对负收益风险的启示：DeepInfer 全量周期重排在**负载漂移/无稳定热点**时同样可能频繁整体搬运，与 STAIR 类实现的"满量重排 = 负收益"诊断一致。调参时应把 `REARRANGEMENT_STEP_INTERVAL` 设大、并谨慎设置 `OVERRIDE_NUM_EXTRA_EXPERTS`（冗余槽位越多,搬运/内存开销越大，仅在 384 不被 EP 整除时才需要）。

---

## 8. 源码逐段解析（含行号标注）

### 8.1 冗余专家决议 — `eplb/__init__.py:21-39`

```python
21  def resolve_num_extra_experts(num_routed_experts, min_num_extra_experts, ep_size) -> int:
22      """取不小于下限、且使 num_physical_experts 能被 ep_size 整除的冗余 expert 数。"""
23      padding = -(num_routed_experts + min_num_extra_experts) % ep_size
24      return min_num_extra_experts + padding
25
31  def get_initial_global_physical_to_logical_map(num_routed_experts, num_extra_experts) -> list[int]:
32      """初始布局：[原始 routed experts, 复制的 routed experts]。"""
35      return [
36          *range(num_routed_experts),                                    # 前 n_routed 槽位 = logical 本身
37          *(expert_index % num_routed_experts for expert_index in range(num_extra_experts)),  # 冗余 = 循环复制
38      ]
```

**逐行**：`padding` 用模运算算出还需补几个槽位才能被 `ep_size` 整除（`-x % ep_size` 在 Python 中结果 ∈ [0, ep_size)）。`get_initial_global_physical_to_logical_map` 生成初始一维映射，冗余槽位 `i % n_routed` 循环复制 routed 专家。这段决定了**初始布局**以及**物理槽位总数为什么能被 EP 整除**（`ExpertLoadBalancer.__init__:75` 有 `assert num_physical_experts % ep_size == 0`）。

### 8.2 全局重平衡 + 跨 rank 放置策略 — `policy.py`

**(a) 冗余副本贪心分配 — `rebalance_experts_global` (policy.py:73-123)**

```python
105      # 贪心分配冗余副本：每轮选一个 expert 复制。
106      # score[..., j] 是 expert j 当前的每副本负载，score_added 是给 j 加一个副本后的每副本负载
109      tokens_per_expert_all_diff = tokens_per_expert + arange_logical * 1e-4   # 打破 tie
110      for i in range(num_redundant_experts):
111          score = tokens_per_expert_all_diff / logical_count     # 每副本负载
112          score_added = tokens_per_expert / (logical_count + 1)  # 若加一份后的每副本负载
113          values, indices = score.max(-1, keepdim=True)          # 当前每副本负载最高的 expert
114          values = values.expand_as(score).contiguous()
115          score.scatter_(-1, indices, score_added.gather(-1, indices))  # 把 argmax 位换成 score_added
116          values.scatter_(-1, indices, score.max(-1, keepdim=True).values)
117          redundancy_indices = values.sum(0).argmin(-1)  # 按 step 求和，选「复制后 max 每副本负载最小」的专家
118
119          physical_index = num_logical_experts + i       # 本轮冗余副本占用槽位
120          physical_to_logical_map[:, physical_index] = redundancy_indices
121          redundancy_count = logical_count.gather(-1, redundancy_indices.unsqueeze(-1)).squeeze(-1)
122          logical_to_physical_map[arange_layers, redundancy_indices, redundancy_count] = physical_index
123          logical_count[arange_layers, redundancy_indices] += 1
```

**核心思想（代码注释 + 逐行）**：
- `score = tokens/logical_count` 是"每个副本平均扛多少 token"，其 `argmax` 是当前最挤的专家；
- 假设给它加一份副本后其每副本负载变为 `score_added = tokens/(count+1)`；
- 把 `argmax` 位置的值替换为 `score_added` 后，该位置的 `max` 即为"复制这个专家后整个 max 每副本负载"（第 116 行 `values.scatter_(..., score.max(...))` 重新算替换后的 max）；
- `values.sum(0).argmin` 对每层选**复制后 max 下降最多的专家**（`sum(0)` 对窗口 step 求和，`argmin` 沿 layer 维度——注意这里语义是"每层独立选一个要复制的专家"）。

这一向量化技巧避免了逐层 Python 循环，整个 `for i in range(num_redundant)` 只是复制轮数。

**(b) 跨 rank 贪心放置 — `_batch_greedy_indices` (policy.py:12-70)**

```python
31      weights = tokens_per_expert.sum(dim=0, dtype=torch.float32) / logical_count
33      physical_experts = physical_to_logical_map.to(torch.int64)
34      physical_order = weights.gather(-1, physical_experts).argsort(dim=-1, descending=True, stable=True)
35      expert_order = physical_experts.gather(-1, physical_order)   # 按每副本总负载降序的处理顺序
36      vectors = tokens_per_expert.permute(1, 2, 0)...               # [layers, physical, steps] 逐 step 负载
37      vectors.div_(logical_count.unsqueeze(-1))                     # 归一成每副本负载向量
...
44      # 目标：min sum_{step} max_{rank} (该 rank 逐 step 负载和)
45      # 贪心：每次把当前 expert 放到「使上述目标最小」的 rank
46      loads = vectors.new_zeros((num_layers, num_ranks, num_steps)) # 各 rank 累计逐 step 负载
47      peaks = vectors.new_zeros((num_layers, num_steps))            # 各 step 最大 rank 负载
48      occupancy = torch.zeros((num_layers, num_ranks), dtype=torch.int64)
49      candidates = torch.empty_like(loads)
50      scores = vectors.new_empty((num_layers, num_ranks))
51      indices = torch.empty_like(physical_order)
53      for job_index in range(num_physical):                         # 逐个 expert 放置
54          experts = expert_order[:, job_index]
55          vector = vectors[layers, experts]                         # 该 expert 的逐 step 负载
58          torch.add(loads, vector.unsqueeze(1), out=candidates)     # 假设放进每个 rank
59          torch.maximum(candidates, peaks.unsqueeze(1), out=candidates)  # 更新后逐 step 峰值
60          torch.sum(candidates, dim=-1, out=scores)                 # Σ_step 峰值 作得分
62          scores.masked_fill_(occupancy == capacity, float("inf"))  # 满 rank 排除
63          rank = scores.argmin(dim=-1)                              # 选得分最低的 rank（tie→最小 ID）
65          indices[layers, rank * capacity + occupied] = physical_order[:, job_index]
66          occupancy[layers, rank] = occupied + 1
67          updated = loads[layers, rank] + vector
68          loads[layers, rank] = updated
69          torch.maximum(peaks, updated, out=peaks)                  # 更新该 step 峰值
70      return indices
```

**逐行**：`indices` 是每层一个排列（每连续 `capacity` 个 = 一个 rank）。外层 `for job_index` 是唯一 Python 循环，内层所有候选计算都用张量操作并行（`loads/peaks/scores` 形状 `[layers, ranks, steps]` 一次性向量化）。目标精确对应注释第 44 行：`min Σ_step max_rank(rank 的逐 step 负载和)`——既平衡平均负载，又因为向量保留完整历史 step，能**跨 batch 互补错开放置热点**。

### 8.3 专家权重的原地重排 — `shuffle.py`

**通用跨卡版 `_shuffle_layer_p2p` (shuffle.py:94-147)**（每层一次，复用接收缓冲）：

```python
108      # 卡内 origin -> 缓冲 的拷贝（不需要远端数据）
109      received, intra_copies = _plan_intra_copies(origin_local, current_local)
110      for src, dst in intra_copies:
111          for buf, weight in zip(recv_buffers, weights, strict=True):
112              buf[dst].copy_(weight[src])                             # 本卡自足拷贝
113
114      # 跨卡 p2p：按 (对端 rank 差 mod 组数) 分桶，逐桶批量收发
115      p2p_buckets: list[list[dist.P2POp]] = [[] for _ in range(envs.DEEPINFER_REARRANGE_P2P_GROUPS)]
116      # 发送：对本卡持有的每个 expert，发给配对到自己名下的 receivers
117      for expert, src in _first_slot_per_expert(origin_local):
118          senders, receivers = _remote_routes(origin, current, num_local, expert)
119          my_index = [rank for rank, _ in senders].index(ep_rank)
120          for j, dst in enumerate(receivers):
121              if _paired_sender(j, len(senders), len(receivers)) == my_index:
122                  p2p_buckets[(ep_rank - dst) % len(p2p_buckets)].extend(
123                      dist.P2POp(dist.isend, weight[src], dist.get_global_rank(ep, dst), ep) for weight in weights)
125      # 接收：对每个缺失 expert 从配对 sender 收进接收缓冲（同一 expert 多槽位只收一次）
127      for expert, dst in experts_to_recv:
...
130          src_rank, _ = senders[_paired_sender(receivers.index(ep_rank), len(senders), len(receivers))]
131          p2p_buckets[(src_rank - ep_rank) % len(p2p_buckets)].extend(
132              dist.P2POp(dist.irecv, buf[dst], dist.get_global_rank(ep, src_rank), ep) for buf in recv_buffers)
135      for ops in p2p_buckets:                                        # 每桶一次 batch_isend_irecv
136          if ops:
137              for req in dist.batch_isend_irecv(ops):
138                  req.wait()
140      # 收尾：缓冲 -> 本卡槽位
141      recv_slot = dict(experts_to_recv)
142      for i in range(num_local):
143          if origin_local[i] == current_local[i]: continue
144          src = i if received[i] else recv_slot[current_local[i]]
145          for weight, buf in zip(weights, recv_buffers, strict=True):
146              weight[i].copy_(buf[src])
```

关键辅助：`_plan_intra_copies`(16) 判卡内自足；`_remote_routes`(28) 求每 expert 的 sender/receiver rank 集；`_paired_sender`(47) 用**同一公式**做收发配对（否则 isend/irecv 对不齐）。

**单机 IPC 镜像版 `_shuffle_layer_intranode` (shuffle.py:178-218)**（限 `is_within_nvl_domain and is_gpu`）：

```python
192      ep.barrier().wait()          # 等所有 rank 读完上一层镜像
193      for m, weight in zip(mirror, weights, strict=True):
194          m.copy_(weight)           # 本卡权重写入 symm_mem 镜像（全员可见）
195      ep.barrier().wait()           # 等所有 rank 镜像就绪
197      # 卡内 origin -> current 拷贝，源从自己镜像读（避免覆盖来源）
198      received, intra_copies = _plan_intra_copies(origin_local, current_local)
199      for src, dst in intra_copies:
200          for weight, m in zip(weights, mirror, strict=True):
201              weight[dst].copy_(m[src])
203      # 远端读取：直接从配对 sender 的镜像拷贝，无需 P2P
204      for expert, dst in experts_to_recv:
...
207          src_rank, src_slot = senders[_paired_sender(...)]
208          received[dst] = True
209          for weight, peer in zip(weights, ipc_experts_weight[src_rank], strict=True):
210              weight[dst].copy_(peer[src_slot])   # 直接读对端镜像
212      # 收尾：同 expert 其余槽位从已填好的第一个槽位复制
213      recv_slot = dict(experts_to_recv)
214      for i in range(num_local):
215          if not received[i]:
216              src = recv_slot[current_local[i]]
217              for weight in weights:
218                  weight[i].copy_(weight[src])
```

`ExpertLoadBalancer._initialize_shuffle`(195) 首次重排时 `empty_cache()` 防 OOM，并 `symm_mem.rendezvous` 建镜像句柄；镜像只占**一层**权重，逐层刷新复用（`ipc_experts_weight` = `ep_size` 个 rank 的镜像缓冲列表）。

### 8.4 调度与统计 — `eplb/__init__.py`

**step / 周期调度 (131-165)**：

```python
131  def step(self, is_prompt) -> None:
133      if self._ep_group.size() <= 1: return       # EP=1 不启用
136      self._update_rearrangement(is_prompt)
137      self._update_load_balance_stats()
138      self.expert_count.zero_()                    # 计数清零

146  def _update_rearrangement(self, is_prompt):
147      if self._rearrangement_interval == 0: return # 0 = 关闭
149      if is_prompt and self._skip_rearrangement_on_prefill: return
152      window_index = self._expert_count_window_index
153      self._expert_count_window[window_index].copy_(self.expert_count)  # 入滑动窗口
154      self._expert_count_window_index = (window_index + 1) % self._expert_count_window.size(0)
156      if is_prompt and self._schedule_rearrangement_after_prefill:
157          self._rearrangement_step = self._rearrangement_interval - 1   # prefill 结束→下一个非 prefill 步重排
158          return
161      self._rearrangement_step += 1
162      if self._rearrangement_step < self._rearrangement_interval: return
164      self._rearrangement_step = 0
165      self._rearrange_experts()
```

**重排主体 `_rearrange_experts` (222-278)** —— 窗口计数聚合 → all_reduce → 策略 → 重排权重：

```python
234      # 窗口内各 logical expert 的 token 数：本地 physical 槽位聚合后全局 all-reduce
235      window_size, num_moe_layers, num_local_physical_experts = self._expert_count_window.shape
238      local_physical_to_logical_map = self.physical_to_logical_map[                     # 本 rank 的槽位映射
239          :, ep_group.rank()*num_local_physical_experts : (ep_group.rank()+1)*num_local_physical_experts]
242      logical_expert_token_counts = torch.zeros(window_size, num_moe_layers, num_logical_experts, ...)
249      logical_expert_token_counts.scatter_add_(-1, local_physical_to_logical_map.to(torch.int64)
250          .expand_as(self._expert_count_window), self._expert_count_window)   # physical→logical 聚合
254      dist.all_reduce(logical_expert_token_counts, group=ep_group)            # 全局合并
256      next_physical_to_logical_map, next_logical_to_physical_map, next_logical_count = rebalance_experts_global(
257          logical_expert_token_counts, num_physical_experts, num_local_physical_experts)
261      if self._intranode_shuffle:  # 单机镜像版
262          expert_model_parallel_shuffle_inplace_intranode(...)
269      else:                       # 跨卡 P2P 版
270          expert_model_parallel_shuffle_inplace(...)
276      self.physical_to_logical_map.copy_(next_physical_to_logical_map)         # 提交新映射
277      self.logical_to_physical_map.copy_(next_logical_to_physical_map)
278      self.logical_count.copy_(next_logical_count)
```

**负载均衡度上报 (167-193)**：每 `DEEPINFER_MOE_LB_STAT_INTERVAL` 步 `all_gather_into_tensor` 各 rank 每层 token 数，得 `(avg, max)` 存 `moe_load_balance_ratio`；`_log_rearrangement_metrics`(290) 在 rank0 打印 `rank_balance / logical_balance / physical_peak / time`。

### 8.5 与模型集成 — `deepseek.py` / `model_loader.py`

```python
# deepseek.py:344-364  按同构 layout 分组建 balancer，注册全局 Manager
for group in grouped_moes.values():
    load_balancer = eplb.ExpertLoadBalancer([weights for _,_,weights in group], len(group),
        example_moe.num_logical_experts, example_moe.num_physical_experts,
        example_moe.ep_group, example_moe.num_routed_experts, example_moe.num_extra_experts,
        is_normal_engine=cfg.engine_type == EngineType.NORMAL)
eplb.set_current(eplb.ExpertLoadBalancerManager(groups, len(moes)))

# deepseek.py:402-409  sample 时推进 EPLB
expert_lb = eplb.current()
if expert_lb is not None:                     # dense 模型无 EPLB
    expert_lb.step(input_metadata.is_prompt)
    def _moe_lb_post_func():
        sampler_output.moe_load_balance_ratio = expert_lb.take_moe_load_balance_ratio()
    sampler_output.post_funcs.append(_moe_lb_post_func)

# model_loader.py:83-100  _configure_moe_redundancy：每个 MoE layout 决议冗余数
def resolve(moe_cfg, layout):
    num_extra = resolve_num_extra_experts(moe_cfg.n_routed_experts, min_num_extra_experts, ep_size)
    ...
main_moe_cfg.n_redundant_experts = resolve(main_moe_cfg, "backbone")
dflash_moe_override.n_redundant_experts = resolve(main_moe_cfg + dflash_moe_override, "DFlash")
```

**集成关键点**：`_configure_moe_redundancy` 在 `finalize_model_config` 里跑，**先于** MoE 构造，写的 `n_redundant_experts` 决定 `FusedExperts.n_routed_experts = logical + redundant`（moe.py:78）进而决定 `ParallelRoutedLinear` 的参数行数；`mlp.py:392` 把 `logical_to_physical_map/logical_count` 传给 dispatch 算子；`mlp.py:474/555` 用 `expert_count` 作为本卡专家接收 token 计数。
