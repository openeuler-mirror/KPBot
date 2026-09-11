---
name: parallel-unique-pytorch
description: >
  工作场景：优化 embedding lookup 流水线（host 侧 raw_id → unique → hashmap → gather）中 CPU 侧的
  unique/sort 瓶颈，用 __gnu_parallel::sort + OpenMP 手写并行 unique 替代单线程的 np.unique/torch.unique，
  并以 TORCH_LIBRARY + register_fake 整合为支持 torch.compile 的 torch.ops.* 自定义算子；也适用于把
  pybind11/numpy C++ 扩展改造成 PyTorch 自定义算子。
  不适用的反例：unique 可搬上 CUDA（直接用 device 版本）、n < 2048（OpenMP 约 50us 启动开销主导，必亏）、
  需要 int32/int64 之外 dtype 的场景。
---

# 并行 Unique 优化 + PyTorch 自定义算子整合

当 profiling 表明 embedding lookup pipeline（host 侧 raw_id → unique → hashmap → gather）的瓶颈落在 CPU 侧的 unique/sort 上时，使用本 Skill。它沉淀了完整的实测方法论：一是用 `__gnu_parallel::sort` + OpenMP 手写并行 unique，替代单线程的 `np.unique`/`torch.unique`；二是把 C++ 扩展以 `TORCH_LIBRARY` + `register_fake` 的方式整合为支持 `torch.compile` 的 `torch.ops.*` 自定义算子。本 Skill 中的所有数字均来自实测（best-of-N 计时、分阶段 breakdown、拐点扫描），不是理论推导，引用时不要凭感觉外推。

## When to use

满足以下任一条件时进入本 Skill 流程：

1. **profiling 显示 `np.unique`/`torch.unique`（CPU）是热瓶颈**：典型形态是 host 侧的 raw_id → unique → hashmap → gather 流水线，unique/sort 在火焰图或分阶段计时中占比显著。
2. **unique 必须留在 host**：下游 hashmap 没有 device 版本，unique 的输出要喂给 host 侧哈希表，搬到 GPU 反而引入传输开销。
3. **需要多线程 sort**：`torch.unique` CPU 路径是单线程实现，需要手写多线程 sort 才能利用多核。
4. **pybind11 + numpy 扩展要改造成 `torch.ops.*`**：已有 C++/numpy 混合扩展，需要升级为支持 `torch.compile` 的 PyTorch 自定义算子。

不适用的情况（明确拒绝，不要套用本 Skill）：

- **unique 能搬到 CUDA**：GPU 上有现成的 unique/sort 实现，直接用 device 版本，不要在 host 上折腾并行。
- **n < 2048**：OpenMP 线程启动约 50us，小规模下启动开销完全主导，并行版必亏。
- **需要 int32/int64 之外的 dtype**：核心实现只按这两种整型分发，其他 dtype 先做不了。

## Core findings

以下 6 条是整个优化的核心结论，动手前先读懂，避免在错误的方向上花时间。

1. **基线是单线程的，这是并行版能赢的全部原因。** `torch.unique` 的 CPU 路径是单线程实现，调用 `torch.set_num_threads()` 对它无效；`np.unique` 同样单线程。也就是说，基线从来没有利用过多核，手写并行版并不是"写得比别人好"，而是基线根本没有竞争——这决定了收益上限。

2. **sort 并行收益显著，但 inverse 是瓶颈，必须分开评估。** `__gnu_parallel::sort` 在 16 线程下可获得 4-14x 加速；但 inverse 阶段（对原始位置做二分查找）是内存带宽瓶颈，并行化只有 1.0-1.1x，几乎白做。n=1M 时 inverse 占整个 op 的 50-67%（机器相关：原实测约 67%，192 核机器复测约 50-53%，但始终是最大的单一阶段），把 op 级总加速稀释到 4-5x。评估收益时看 op 整体，不要只盯 sort 阶段的高倍数。

3. **pipeline 级归因：85% 的收益来自 cache locality，不是来自 sort 变快。** 在完整 pipeline 中 sort 仅占 0.2-2.2%，hashmap 才占 90%+；并行 sort 对 pipeline 的直接贡献只有 1.06-1.28x。真正的收益来源是排序让 hashmap 的访问从随机变顺序，cache locality 大幅改善，单次 lookup 从 0.42us 降到 0.21us——这部分贡献了 85% 的 pipeline 收益，仅 15% 来自查询次数减少。正确的心智模型：并行 sort 的价值是把 unique 的入场费降到噪声级，使得 dup=10% 这种场景也值得做 unique，而不是"sort 更快 → pipeline 更快"。

4. **拐点由 n = batch × seq_len 决定，与单独的 batch/seq 取值无关。** 两个小维度相乘后落在哪个区间才是判断依据：n < 2048 全亏；2048-4096 临界；>= 4096 开始赢；>= 65536 可拿到 1.4-3.0x 的 pipeline 收益。

5. **concat-then-unique 永远最优，per-batch unique 双重亏损。** 先把所有 batch concat 再做一次全局 unique：一是 per-batch 拆分会丢掉全局去重空间（global dup=0.10 拆成 256 个 batch 后 per-batch dup 趋近 0，等于白做）；二是 N 倍 OpenMP 启动开销（256 × 50us = 12.8ms，比 sort 本身还贵）。

6. **线程数必须封顶，禁止直接用 `omp_get_max_threads()`**——在大机器上它可能返回 384，线程过多反而劣化。实测最优封顶值：

   | n（= batch × seq_len） | 最优线程数 |
   |---|---|
   | n <= 4096 | 8 |
   | n <= 65536 | 16 |
   | n <= 1M | 16 |
   | n > 1M | 32 |

   **注意：上表封顶值是机器相关的，必须按目标机器重调。** 192 核机器复测发现 64t 最优（n=1M 时 19ms），原表 16t 偏保守（29-35ms）；但全开 `omp_get_max_threads()`（192t，23.8ms）确实劣于调优后的封顶值——"必须封顶"的原则不变，可迁移的是"封顶且远小于 `omp_get_max_threads()`"，具体数值上线前在目标机器上按 Methodology 步骤 3 的扫描法重测。

## Methodology

按以下 5 步执行，每步都有明确产物，缺产物视为该步未完成。

1. **先测全 pipeline 各阶段耗时，别只测 op。** 跳过这一步容易在只占 0.2% 的阶段上白干。产物：pipeline 分阶段耗时表。
2. **从第一天就暴露 `*_timed` 分阶段接口。** 后续所有归因和调参都依赖它，事后补做等于重写。产物：接口返回 `timings=[sort, dedup, prefix, compact, inverse, total]`（单位 ms）。
3. **测拐点：扫 n_elements × dup，同环境三路径对比。** 三路径为 A_np（numpy 基线）/ A_par（并行版）/ B（不做 unique），收益判断看 par/B 而不是 par/np——B 路径才是"要不要做 unique"的真正对照。产物：n × dup 扫描矩阵与 par/B 比值表。
4. **算法按 5 阶段实现：`__gnu_parallel::sort` → 并行 mark-first → 并行 prefix sum → 并行 compact → 并行 inverse。** 选 `__gnu_parallel::sort` 的理由：C++17 PSTL 依赖 TBB（环境里经常装坏），numba 的 sort 仍是单线程，pyarrow 不返回排序后的 unique，三者在实测中都被排除。产物：并行 unique 实现 + 选型记录。
5. **整合进 PyTorch，按下面 checklist 执行。** 产物：`torch.ops.*` 自定义算子，支持 `torch.compile`。

## Pytorch integration checklist

### C++ 结构（4 步）

按以下分层组织 C++ 代码，core 与 torch 解耦，每层职责单一：

1. **core 模板 `<T>`**：纯 C++ 核心实现，`template<class T>`，不依赖任何 torch 类型。便于单独测试和复用（pybind11 POC 阶段直接调用同一份 core）。
2. **dispatch 层**：从 `at::Tensor` 取 `data_ptr` 桥接到裸指针，调用 core。这一层是 torch 与 core 之间唯一的粘合点，不做业务逻辑。
3. **impl 层**：入参校验 + dtype 分发。CUDA 输入、float dtype、超过 2 维等非法输入直接抛 `RuntimeError`；合法输入按 int64/int32 分发到 `core<int64_t>` / `core<int32_t>`。
4. **TORCH_LIBRARY 注册**：`def` schema 并绑定 impl，注意下面两个陷阱。

### 两个注册陷阱（必避开）

**⚠️ CRITICAL：Trap A —— `m.impl(name, fn)` 不带 dispatch key**

不带 dispatch key 的 `m.impl` 会把算子注册到 **CompositeImplicitAutograd**；随后 `register_fake` 会报错：

> already has an implementation ... CompositeImplicitAutograd operators do not need a fake impl

修复：绑定 impl 时显式带上 CPU dispatch key：

```cpp
m.impl("unique_par", c10::DeviceType::CPU, &unique_par_cpu);
```

**⚠️ CRITICAL：Trap B —— `m.def(schema, "docstring")` 第二参数类型变了**

PyTorch 2.9 起 `m.def` 的第二参数是 `std::vector<Tag>` 而不是 string，传 docstring 会导致模板推导失败、编译不过。

修复：`m.def(schema)` 只传 schema 一个参数，docstring 放到 Python 侧。

### register_fake（data-dependent output size，用 unbacked SymInt）

unique 的输出规模在编译期不可知（data-dependent output size），fake impl 必须用 unbacked SymInt 表达 `n_unique`，并用 `_constrain_range_for_size` 约束上界，否则 `torch.compile` 无法 trace。以下代码片段原样保留：

```python
@register_fake("unique_par::unique_par")
def __fake(self, n_threads=0):
    fm = maybe_get_fake_mode(self); numel = self.numel()
    if not has_free_symbols(numel) and numel == 0: n_unique = 0
    elif fm is None or fm.shape_env is None: n_unique = 0
    else:
        n_unique = fm.shape_env.create_unbacked_symint()
        maxval = int(numel) if not has_free_symbols(numel) else sys.maxsize-1
        _constrain_range_for_size(nunique, max=maxval)
    return self.new_empty((n_unique,)), self.new_empty(self.shape, dtype=torch.long)
```

这是 `torch.unique` 自己的 fake impl 的同一 idiom（见 `torch/_subclasses/fake_impls.py::_unique`）。注意：以上片段为原始记录原样保留，落地实现时 `_constrain_range_for_size(nunique, ...)` 的变量名需与上文 `n_unique` 保持一致。

### setup.py

- 扩展使用 `CppExtension` + `-fopenmp`。
- PyTorch Linux wheel 自带 libgomp，与系统 libgomp 无冲突，不需要额外处理。
- `<parallel/algorithm>` 由 GCC 自带，无需引入 TBB 等额外依赖。

### `__init__.py` 加载顺序

按固定顺序加载，各步骤失败语义不同：

1. 先 load 编译产物 `.so`。
2. 注入 `torch.unique_par`（Python 侧包装，docstring 放这里）。
3. 最后 register + `register_fake`：这一步**单独 try/except**，失败只 warn 不阻塞主功能，保证注册失败时核心功能仍可降级使用。

### 测试最小集

整合完成后必须通过以下最小测试集，缺一不可：

1. **正确性对比**：与 `torch.unique(return_inverse=True)` 的输出做 `torch.equal` 对比，覆盖多分布 × int64/int32 × 1D/2D。
2. **错误输入**：CUDA / float / 3D 输入必须抛 `RuntimeError`。
3. **timings 自洽**：`timings` 各阶段之和与 total 校验一致。
4. **torch.compile**：trace 通过，fake impl 生效。
5. **与旧 numpy 扩展一致性**：改造前后的输出完全一致。

## Anti-patterns

以下 9 条反模式均有实测或编译期证据，遇到即拒绝：

| # | 反模式 | 为什么错 | 正确做法 |
|---|--------|---------|---------|
| 1 | per-batch unique | 双重亏损：丢全局去重空间（global dup=0.10 拆 256 batch 后 per-batch dup~0）+ 256×50us=12.8ms 的 OpenMP 启动开销 | concat-then-unique，一次全局去重 |
| 2 | `m.impl(name, fn)` 不带 dispatch key | 注册到 CompositeImplicitAutograd，`register_fake` 直接报错 | `m.impl(name, c10::DeviceType::CPU, fn)` |
| 3 | `m.def(schema, "docstring")` | PyTorch 2.9 第二参数是 `vector<Tag>`，模板推导失败 | `m.def(schema)`，doc 放 Python 侧 |
| 4 | OpenMP 线程不封顶 | `omp_get_max_threads()` 可能返回 384，线程过多反而劣化 | 按 n 封顶：<=4096→8t，<=65536→16t，<=1M→16t，>1M→32t |
| 5 | `register_fake` 返回静态 size | unique 输出是 data-dependent size，静态 size 会让 `torch.compile` 产生错误假设 | 用 unbacked SymInt + `_constrain_range_for_size` 约束上界 |
| 6 | 只优化 sort 忽略 inverse | inverse 是内存带宽瓶颈（并行仅 1.0-1.1x），n=1M 时占 67%，决定 op 总加速上限 | 分阶段计时，op 级评估收益 |
| 7 | 把 pipeline 收益归因于 "sort 更快" | 85% 收益来自 cache locality（hashmap 访问随机→顺序，0.42us→0.21us），sort 变快只占直接贡献的少数 | 心智模型：并行 sort 把 unique 入场费降到噪声级 |
| 8 | numpy roundtrip | torch Tensor ↔ numpy 来回拷贝破坏 `torch.compile` 兼容，且引入额外开销 | dispatch 层直接从 `at::Tensor` 取指针 |
| 9 | 只比 A_par vs A_np，不带 B 路径 | par/np 只能证明"比单线程 unique 快"，回答不了"该不该做 unique" | 同环境跑三路径 A_np/A_par/B，以 par/B 为准 |

## Benchmark protocol

所有性能结论必须按以下协议测得，否则数字不可信：

- **best-of-N**：每组取 N 次运行的 min，不是 mean——min 才排除调度噪声，mean 会被系统抖动污染。
- **breakdown 取 min-total 那次**：分阶段耗时取总耗时最小的那次运行的分解，而不是各阶段分别取 min（各阶段 min 来自不同运行，加起来没有意义）。
- **三路径同环境**：A_np / A_par / B（不做 unique）必须在同一机器、同一负载下对比，结论看 par/B。
- **扫 n × dup**：拐点和收益区间必须用 n_elements × dup 的扫描矩阵确定，不允许拿单点数据下结论。
