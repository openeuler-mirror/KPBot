---
name: tf-inference-optimizer
description: TF 推理优化统一工作流（主入口），编排「环境验证 → 基线建立(极限测试) → 多维 profiling(静态图/trace/perf) → 优化迭代(逐优化点 subagent) → 报告」全流程，含滚动 baseline、候选池多维路由、外部构建协作、正确性用例硬门控、负收益根因分析。适用于鲲鹏 aarch64 上的 TF 2.20 自研 fork（KDNN/ANNC）。触发：TF 推理优化、推理性能优化、优化全流程、鲲鹏推理调优、图优化+kernel 优化工作流。
---

# TF 推理优化统一工作流

这是鲲鹏 aarch64 上 TF 推理优化的端到端流程编排。按五阶段串联以下子 skill，每阶段用对应 skill 的详细方法。

## 子 skill 地图

| 阶段 | skill | 产出 |
|---|---|---|
| 0. 环境验证 | `tf-env-verify` | 环境就绪确认（含环境信息询问记录） |
| 1. 基线建立 | `tf-benchmark` | 端到端极限测试 → baseline 容量/P99/CPU |
| 2. profiling | `tf-profile-collector` | 静态图 + trace + perf 多维画像 |
| 3. 优化迭代 | `tf-graph-optimize` + `tf-kernel-optimize` + `tf-result-verify` + `tf-benchmark` | 逐优化点设计/实施/验证 |
| 4. 报告 | —（写 `results/OPTIMIZATION_WORK_SUMMARY.md`） | 工作总结 + 候选点 |

## 分层架构与 backend 抽象

优化动作按三层落地，接口清晰、互不耦合，便于换后端/换平台时复用上层逻辑：

```
┌─ 编排层 ──┐  tf-inference-optimizer（状态机 + 门控 + 候选池路由）   ← 与 backend 无关
├─ 图优化层 ─┤  tf-graph-optimize（remapper / rewriter 模式匹配）        ← 依赖 TF Grappler 接口
├─ kernel 层 ─┤  tf-kernel-optimize（REGISTER_OP / REGISTER_KERNEL_BUILDER）
└─ 后端库层 ─┘  KDNN / ANNC / NEON / SVE（grouped GEMM、post-op、prepack）
```

- 后端库层对上层只暴露能力（grouped GEMM、post-op、prepack 等），kernel/图优化层通过抽象调用，不直接依赖某后端实现细节。
- 换后端（如 KDNN → 其它 BLAS / 自研库）或换平台（aarch64 → 其它架构）时，只动「后端库层」和部分「kernel 层」，编排层与图优化模式匹配逻辑不变。
- 详见 `references/backend-abstraction.md`。

## 流程总览

```
阶段0 环境验证   阶段1 基线建立     阶段2 profiling     阶段3 优化迭代(循环)   阶段4 报告
┌────────────┐ ┌───────────────┐ ┌─────────────────┐ ┌────────────────────┐ ┌─────────┐
│ env-verify │→│ benchmark     │→│ profile-collector│→│ 逐优化点 subagent   │→│ 工作    │
│ 环境询问   │ │ 极限测试      │ │ 静态图+trace+perf│ │ 设计→确认→实施→commit│ │ 总结   │
│ 链路自检   │ │ 容量扫描      │ │ 交叉验证(两维)   │ │ →外部构建→验证→根因  │ │        │
└────────────┘ └───────────────┘ └─────────────────┘ └────────────────────┘ └─────────┘
     GATE①         GATE②             GATE③                 GATE④(每点)
   环境就绪      基线已建立        profiling已确认           正确性用例通过
```

## 产物与状态持久化（统一存 `results/`，防中断遗漏）

⚠️ 所有工作流产物（状态、profiling、trace、perf、设计、验证结果、报告）统一存到**工作目录根下的 `results/` 目录**，禁止散落到 `.opencode/` 等隐藏目录——方便用户查阅和校验。禁止只记对话上下文。

```
results/
├── baseline_state.json            # 滚动 baseline（初始 baseline + 已合入优化点继承清单）
├── optimization_points_state.json # 各优化点状态机（designed→committed→deployed→accepted/rejected）
├── collection_plan.json           # 采集计划（带理由）
├── graph_profile.json             # 静态图画像
├── baseline_profile.json          # 动态基线
├── cross_validation.json          # 交叉验证结论
├── profiling_report.md            # profiling 报告（方法/结果/分析）
├── metadata_baseline/             # trace：baseline op 级耗时
├── metadata_<point_id>/           # trace：各优化点
├── perf_baseline/                 # perf：baseline 采样
├── perf_<point_id>/               # perf：各优化点
├── design/                        # 优化点设计提案
│   └── <point_id>.md
├── rounds/                        # 优化点验证结果
│   └── round_N_<point_id>_summary.json
└── OPTIMIZATION_WORK_SUMMARY.md   # 工作报告
```

每个关键节点写回对应文件，任务中断后可据此续跑。`baseline_state.json` / `optimization_points_state.json` 的字段与状态转移定义见 `references/state-schema.md`（机器可读 schema，非法状态转移需拦截）。

## 阶段 0：环境验证（内联，`tf-env-verify`）

调用 `tf-env-verify` 完成：环境信息询问（含源码路径 + 源码分支确认）→ 测试方法确认 → 拷贝脚本模板并填充凭据 → 写 `./AGENTS.md` → 链路自检 → binary 部署验证（nm 符号）→ 进程/端口/gflags → server ready。

**GATE① 环境就绪**：环境信息已记录、链路通、kernel 符号非 0、无残留进程、server ready。未过即停。

## 阶段 1：基线建立（端到端极限测试，`tf-benchmark`，必须先于 profiling）

> ⚠️ **先测极限，再 profiling**：不要直接上 profiling。先用 `tf-benchmark` 做端到端极限测试（容量扫描），确定当前 baseline 的容量/P99/CPU 基线数据。**没有基线数据，后续所有优化收益都无法归因**（最后会发现「连基线都没有」）。

执行：

- 端到端压测（限 QPS）：记录各档 P99、CPU。
- QPS 容量扫描：找饱和点（`actual_qps` 落后 + CPU 逼近 80% + P99 跳高）。
- 产出 baseline 性能数据：容量上限 ~N QPS、P99、CPU，写入 `baseline_state.json` 的 `initial_baseline`。

**GATE② 基线已建立**：已产出当前 baseline 的容量/P99/CPU 基线数据。未建立不得进入 profiling。

## 阶段 2：profiling（多维采集，`tf-profile-collector`，基于已建立的基线）

> ⚠️ **baseline 认知（易错，务必理解）**：
> - baseline 不是「无优化」的同义词，而是**本轮优化的对照起点**，即「当前已合入的最新优化状态」。
> - 带加速 flag 的版本（如 `--enable_kdnn=true --annc=true --annc_fused_matmul=true`）是**已经优化后的状态**，它就是进一步优化的 baseline——不要因为它叫 "opt" 就当成最终结果，更不要认为「带 baseline flag 才是 baseline、带 opt flag 就是 opt」。
> - 无加速 flag 的版本是「原生基线」，仅在首次评估整体加速比时对照使用。
> - 判断标准：**看「当前已合入了哪些优化点」，不看 flag/binary 名叫 baseline 还是 opt**。

调用 `tf-profile-collector` 完成：采集计划（带理由，整体确认）→ 全部采集走 subagent（静态图 / trace / perf / topdown / objdump / 多线程）→ 交叉验证 → 产出 profiling 报告（方法 / 结果 / 分析）供用户 review。采集时 baseline 侧必须用「当前已合入的最新优化状态」对应的 binary + flag，而不是无加速的原生版本。

**GATE③ profiling 已确认**：`profiling_report.md`（方法/结果/分析）+ `graph_profile.json` + `baseline_profile.json` + `cross_validation.json` 已落盘，**用户已 review 确认 profiling 结论** 与 baseline（确认的是「当前已优化状态」，不是原生无加速版本）。未过即停。

## 阶段 3：优化迭代（核心）

### 3.1 候选池生成（主 agent）

从 `graph_profile.json`（融合模式 + 命中/拒绝统计）× `baseline_profile.json`（热点）× `cross_validation.json`（瓶颈定位）按多维路由生成候选池，详见 `references/candidate-catalog.md`。决策树见 `references/workflow-gates.md`。

> ⚠️ **候选池是参考，不是硬性要求**：`candidate-catalog.md` 里的 R0-R7/A-E/QKV 是脱敏案例的静态种子，仅供启发。实际候选点必须由**本轮动态证据**（graph_profile × baseline_profile × cross_validation）推导，可增删、可偏离静态目录；静态目录没列出的优化点，只要动态证据支持，同样可进入候选池。

### 3.2 滚动 baseline

- 初始 baseline = 阶段 1 基线建立测得的容量/P99/CPU 数据（当前已优化状态，如带 KDNN/ANNC flag 的版本，而不是无加速的原生版本）。
- 每个优化点 `accepted` 后，其 after 状态升级为新 baseline，写回 `baseline_state.json`（`merged_points` 继承清单）。
- 每个 subagent 任务包携带 `baseline_ref` + `inherited_changes`，强制在上一轮有效配置之上叠加验证，禁止静默回退到初始基线。
- ⚠️ **baseline 随优化点滚动**：A 合入后，A 的产物就是新 baseline；E 合入后，A+E 就是新 baseline。baseline 是「当前已合入状态」，永远不是某个固定 flag 名。

### 3.3 优化点 subagent（串行，一个点一个 subagent）

按 `priority` 串行处理候选池。每个优化点用 `task` 工具**动态创建 subagent**（通用 subagent 类型，`prompt` 注入任务包 + 要加载的 skill 序列），分「设计 + 实施」两步：

```
① 设计（subagent）：产出设计提案 results/design/<point_id>.md（图优化方案 + kernel 方案 + 风险 + 验证计划）
② 用户确认门控：认同 → 实施；不认同 → 交流讨论重新设计
③ 实施（subagent 续会话）：写代码 → 静态自检 → （有本地构建能力时先本地编译+单测，见 tf-graph-optimize 第6节）→ commit
④ 告知用户 push + 构建（构建由开发者/CI 完成，见 3.4）
⑤ 部署后验证（单独验证 subagent）：正确性用例 + 三口径收益 + 负收益根因
⑥ 更新 optimization_points_state.json + baseline_state.json
```

### 3.4 外部构建/部署协作协议

```
commit 后告知用户："可 push 并构建；构建问题请回传给我定位修复"
  ├─ 有构建问题（语法/链接错误）→ 定位修复 → 修复 commit rebase 进之前的特性 commit
  └─ 无问题 → 用户部署完成后通知 → 触发验证
```

### 3.5 负收益根因分析

负收益（≤0%）不立即回退，先复测确认，再根因定位（噪声 vs 真实开销），给出证据链，判断开销能否消除。可消除 → 追加修复轮次（计入同一优化点轮次）；不可消除 → rejected（有据）。详见 `tf-result-verify`。

### 3.6 续跑门控

**每个优化点完成后，询问用户是否启动后续候选优化点**。所有候选优化点都遵循同一优化执行流程（设计→确认→实施→外部构建→验证）。用户选择停止则进入阶段 4。

## 阶段 4：报告（内联）

汇总各优化点结果，三口径交叉印证，诚实归因，手写 `results/OPTIMIZATION_WORK_SUMMARY.md`（贴合现有风格）。结构见 `references/workflow-gates.md` 报告节。下一步计划必须包含未做候选点（供下一轮迭代复用）。

## 关键原则

1. **先验证环境，再动手优化**：binary 没编译进 kernel、有残留进程、gflags 不对，都会让后续验证白费。
2. **先测极限建立基线，再 profiling**：没有基线数据就优化，最后无法归因收益。
3. **baseline 滚动**：每个优化点基于上一优化点合入后的新 baseline 实施；baseline = 当前已合入状态，不是某个固定 flag（不要认为「带 baseline flag 就是 baseline」）。
4. **优化点逐个落地、逐个验证**：每个优化点单独 commit + 单独验证，单变量原则。
5. **正确性是硬约束**：正确性用例（口径由用户定义）全部通过优先于性能；浮点重排不可接受。
6. **三口径交叉印证**：op 级（trace）、端到端（benchmark）、容量（QPS 扫描）三者互相印证。
7. **负收益先根因分析，不简单回退**：区分噪声与真实开销，避免错误丢失重要优化点。
8. **热路径日志必须清理**：kernel Compute 的调试日志验证后删除。

## References

- `references/workflow-gates.md` — 硬门控 + 候选池决策树 + 报告结构
- `references/candidate-catalog.md` — 候选优化点映射（R0-R7/A/E/QKV，参考种子）
- `references/state-schema.md` — 状态机 JSON schema（baseline_state / optimization_points_state 字段与状态转移）
- `references/backend-abstraction.md` — 编排/图优化/kernel/后端库 四层抽象
- `references/commit-standard.md` — 提交规范（rebase 规则）
- `references/optimization-work-summary.md` — 端到端优化案例（A/E/QKV 收益 + 教训，脱敏）
- `references/optimization-plan.md` — R0-R7 优化点代码位置映射（脱敏）
