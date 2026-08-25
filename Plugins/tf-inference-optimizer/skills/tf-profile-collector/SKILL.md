---
name: tf-profile-collector
description: TF 推理多维性能采集编排。生成带理由的采集计划，通过 task 工具动态创建 subagent 采集 saved_model.pb 静态图、op 级 trace（必采）、服务端请求耗时分解日志/perf/topdown/objdump/多线程/内存带宽（按需），全量 subagent 采集，产出 profiling 报告（方法/结果/分析）供用户 review，并做证据链条与交叉验证。触发：profiling、性能采集、静态图分析、trace、perf、topdown、objdump、内存带宽、内存布局、分配规划、热点分析、维度画像、请求耗时分解、saved_model.pb、交叉验证、profiling 报告。
---

# TF 推理优化 - 多维性能采集编排

本 skill 是 profiling 采集的统一入口，覆盖 8 大优化维度（Op 算子融合 / 调度 / CPU 亲和性 / 内存分配 / 内存连续性 / 内存带宽 / 内存布局与分配规划 / Op 计算效率）+ 请求级瓶颈定位。采集方法分四份 reference，按需读取：

| 方法 | reference | 是否必采 | 执行方式 |
|---|---|---|---|
| saved_model.pb 静态图分析 | `references/static-graph.md` | 是（server 启动前） | task 动态 subagent |
| op 级 trace | `references/trace.md` | 是 | task 动态 subagent |
| 服务端请求耗时分解日志 | `references/server-request-breakdown.md` | 按需（环境确认存在时优先采集） | task 动态 subagent |
| perf/topdown/objdump/多线程 | `references/perf.md` | 按需 | task 动态 subagent |

参考案例（脱敏，作为方法落地实证）：
- `references/model-static-analysis.md` — saved_model.pb 静态图分析实证案例
- `references/trace-capture-sop.md` — trace 抓取标准流程 SOP

## 0. 采集计划生成（带理由，先于任何采集）

进入本 skill 后，先产出一份采集计划 `collection_plan.json`，**每一项必须写明「为什么采集」**，整体一键确认后再执行：

```json
{
  "items": [
    {
      "id": "graph_static",
      "method": "static-graph",
      "subagent": true,
      "reason": "静态解析 saved_model.pb 获取全图结构+计算维度(FLOPs/权重/MatMul分布)，是理解算子推理链与识别融合机会的前提；server 启动前本地即可执行，无需远程"
    },
    {
      "id": "trace_hotspot",
      "method": "trace",
      "subagent": true,
      "reason": "必采：定位真实热点 op，与静态图融合模式交叉得出「既热又可优化」目标"
    },
    {
      "id": "server_breakdown",
      "method": "server-request-breakdown",
      "subagent": true,
      "reason": "环境验证阶段已确认目标服务存在逐请求耗时埋点：直接实测请求解析/预测/序列化各阶段占比，判断瓶颈在框架层(预处理)还是模型层(TF计算)，避免在非瓶颈阶段白费优化"
    },
    {
      "id": "perf_ipc",
      "method": "perf",
      "subagent": true,
      "reason": "仅当 trace 命中 MatMul 热点且静态图显示多个 MatMul 时触发：用 perf stat 查 IPC 判断是「计算效率低」还是「单纯调用多」，避免误判优化方向"
    }
  ]
}
```

**理由书写规范**：每个 `reason` 必须回答「采集它 → 得到什么信号 → 支撑哪个优化维度/结论」，禁止只写工具名。

## 1. 维度 → 采集手段路由表

| 优化维度 | 关键信号 | 采集手段 |
|---|---|---|
| 请求级瓶颈定位 | 请求解析/预测/解压/序列化各自耗时占比 | 服务端耗时埋点日志 |
| Op 算子融合 | 独立 op 数量、op 级耗时分布、dispatch 开销 | trace |
| 调度优化 | inter/intra 并发、run queue、线程切换、shard 数 | trace + 多线程 |
| CPU 亲和性 | 上下文切换、迁移核数、NUMA local/remote | perf |
| 内存分配 | malloc/memcpy 耗时、分配次数、缺页 | perf record |
| 内存连续性 | cache miss、TLB miss、AoS/SoA 布局 | perf stat |
| 内存带宽 | 算术强度(FLOP/byte)、DRAM/总线带宽占用、roofline 位置 | perf 平台事件 + STREAM/likwid |
| 内存布局/分配规划 | TF 内存分配器(buffer 复用/碎片)、算子间临时 buffer、预取/大页 | trace + RunMetadata(step_stats/cost graph) + BFC allocator |
| Op 计算效率 | IPC、指令集使用（NEON/SVE）、kernel 实现 | perf stat + objdump |

## 2. 执行方式（全部 subagent）

profiling 采集（含静态图、trace、perf、topdown、objdump、多线程）都是**消耗上下文的操作**，一律通过 `task` 工具动态创建 subagent 执行。subagent 内消化原始大产物，只回传精简 JSON 结论；主流程只做编排、路由、交叉验证与报告汇总，避免撑爆主上下文。

| 采集 | subagent 内做什么 |
|---|---|
| 静态图分析 | 解析 saved_model.pb，回传节点/FLOPs/融合模式清单 |
| trace 抓取 | 抓取 + 聚合 op 耗时，回传 Top-N 精简结论 |
| 服务端耗时埋点日志 | 读取逐请求分解字段，聚合各阶段耗时占比，回传阶段瓶颈结论 |
| perf record 火焰图 | 消化采样数据成热点函数清单 |
| objdump 反汇编 | 判定「是否命中 NEON/SVE」后回传结论 |
| topdown / 多线程 | 聚合事件表回传指标 |

动态 subagent 通过 `task` 工具创建，用通用 subagent 类型，`prompt` 注入采集方法与回传契约：

```
task({
  description: "静态图分析",
  subagent_type: "general-purpose",
  prompt: "读取 references/static-graph.md 方法，解析 saved_model.pb，回传 graph_profile.json 精简结论"
})
```

**不使用预定义的 subagent 类型**，每个采集项按需动态创建。

## 3. 证据链条 + 交叉验证（防偏角度）

### 3.1 证据链条（每个结论可追溯）

每个采集结论必须带 `evidence` 字段，指向具体来源：

```json
{
  "finding": "MatMul 为头号热点且可融合",
  "evidence": [
    { "source": "graph_profile.json", "metric": "MatMul 28 个, 权重 shape 明确", "dim": "op_fusion" },
    { "source": "trace metadata_baseline", "metric": "MatMul total 2353us/req, 热度第1", "dim": "op_fusion" },
    { "source": "perf stat", "metric": "IPC=1.4", "dim": "op_efficiency" }
  ],
  "cross_validated": true
}
```

### 3.2 交叉验证规则（分级）

| 结论类型 | 印证要求 |
|---|---|
| **瓶颈定位类**（如「XX 是瓶颈」） | **强制**两维印证 |
| 其余（融合模式识别、单点观察） | 从宽，可单维出 `pending_verify` |

规则：
- 两维印证才成结论；单一维度信号只能标 `待验证`。
- 静态图（结构/FLOPs/融合模式）× trace（热点）→ 定位「既热又可优化」。
- 矛盾必须澄清：trace 说某 op 热、但 perf 说 IPC 高且 CPU 低 → 优先怀疑采集口径/负载噪声，复测后再下结论。

## 4. profiling 报告与用户 review（硬门控）

所有采集完成后，主 agent 汇总产出一份 **profiling 报告文档** `profiling_report.md`，必须包含三部分，缺一不可：

1. **采集方法**：本次执行了哪些采集项（方法、命令/参数、执行方式、采集范围）。
2. **抓取结果**：各维度采集的原始关键数据（Top-N op、IPC、cache miss、热点函数、融合模式等，含证据来源）。
3. **结果分析**：交叉验证后的结论（每条结论带证据链 + 结论等级 `conclusion` / `pending_verify`，矛盾项要显式澄清）。

产出后**必须要求用户 review**，未经用户确认不得进入阶段 2 候选池生成：

- 用户确认结论正确 → 进入阶段 2。
- 用户质疑 / 指出矛盾 → 讨论，必要时补采修正，重新 review，直到确认。

> 目的：让结论可被 review 验证（方法可复现、结果有据、分析有链），避免「结论随意、无法追溯」。

## 5. 落盘（统一存工作目录根下 `results/`）

⚠️ 所有 profiling 产物存到工作目录根下的 `results/` 目录，禁止散落到 `.opencode/` 等隐藏目录，方便用户查阅和校验。

| 文件 | 来源 | 说明 |
|---|---|---|
| `results/collection_plan.json` | 主 agent | 采集计划（带理由） |
| `results/graph_profile.json` | subagent | 静态图画像 |
| `results/baseline_profile.json` | subagent | 动态基线（trace 必采 + 按需深采） |
| `results/cross_validation.json` | 主 agent | 交叉验证结论 |
| `results/profiling_report.md` | 主 agent | 采集方法 + 抓取结果 + 结果分析（供用户 review） |

命名约定：trace 目录 `results/metadata_<阶段|优化点id>`，perf 输出 `results/perf_<阶段|优化点id>`，统一 `results/*_<阶段|优化点id>` 隔离多优化点归因。
