# 迭代执行规则

本文件是 `workflow.md` 迭代执行阶段的展开版，定义候选 skill 列表的轮次循环、执行验证 subagent、输出要求、单 skill 停止判定和二进制验证规则。

## 单变量原则 / Single-Variable Principle

所有迭代优化必须遵守单变量原则：

- **定义**：每个执行轮次只能变更来自一个候选 skill 的变量。一个轮次对应一个 skill 的一组绑定动作，不得将多个 skill 的动作合并到同一轮次。
- **重启分离规则**：当多个 skill 的变更都要求同一次应用重启或系统重启时，仍必须拆分为独立轮次，每轮依次实施一个 skill 的变更，并在每轮间执行完整的基准验证（包括重启、warmup、压测和收益计算），以隔离每个 skill 的独立收益贡献。
- **不可归因标记**：若因实际限制（如无法在重启间执行压测、厂商固件捆绑更新等）必须合并执行，合并结果必须在 `per_skill_gain_summary` 中标记为 `confounded`，且 `attribution_method=merged_unresolvable`，并说明无法拆分的原因。
- **例外豁免**：仅允许在 `change_mode=online`（无需重启的动作）之间进行同一轮的多 skill 组合，且组合结果必须在报告中标注 `confounded`。组合仅限第一轮探索，后续必须拆分为独立轮次隔离验证。

## 迭代优化循环

基线确认、瓶颈识别、深度证据采集和 `candidate_skill_list` 生成后，主 skill 进入显式的轮次循环：

1. 从 `candidate_skill_list` 选择当前 skill，先执行 `phase=evidence_candidate`，再执行 `phase=coverage`。
2. 从该 skill 的 `candidate_pool.candidate_actions` 中选择本轮动作。
3. 校验本轮动作是否落在已确认的 `agent_action_mode`、`execution_authorization_scope`、权限范围、回退方式和验证窗口内。基线确认后不得按每个 skill 或每轮动作重复向用户询问批准；只有动作超出已确认边界、风险升级或需要新增权限时，才设置 `scope_change_confirmation_required=true` 并回到用户确认门控。
4. 启动一个执行验证 subagent，并把本轮候选动作、基线、上一轮有效配置、验证命令和回退条件写入任务包。
   生成任务包时必须传入当前 `per_skill_iteration_state`；若该 skill 已验证完全 subskill 给出的所有推荐，任务生成器必须拒绝继续生成执行任务。
5. 执行验证 subagent 确认只有自己会修改测试环境。**环境隔离（默认强制全克隆共用方案）**：进入候选 skill 迭代阶段前，主 agent 先执行 `scripts/manage_verify_env.sh create --source <conda-env-path> --name <src>-verify` **一次性**克隆源 conda 环境为独立验证环境（整个迭代阶段只克隆一次）。**所有候选/coverage subskill 的全部后续分析、实施、复测和回退轮次都共用这一隔离克隆环境执行**，原环境零接触，彻底消除"回退后与原版不一致"风险。全部候选/coverage skill 验证完成后返回用户确认门控，由用户选择：全部回退（`manage_verify_env.sh destroy --name <src>-verify --force` 销毁克隆环境）或应用原环境（`conda list --explicit`/`pip list` 对比 diff → `pip/conda install` 到原环境；环境变量/绑核类变更按记录的 forward_cmd/reverse_cmd 手动应用，并执行 `record-execution`）。隔离为强制默认（`isolation_policy=planA_full_clone_shared`）；用户显式选择其他策略时记录 `isolation_policy=<user_choice>`。克隆不可用或不适用时降级为每轮 `forward_cmd`/`reverse_cmd` 回退并标注 `isolation_degraded=true`；降级时仍以每轮 `forward_cmd`/`reverse_cmd` 记录为回退依据。
5b. **环境使用手册（Environment Handbook，强制）**：共享克隆环境创建后，主 agent 必须生成/确认环境事实文档（`verify_env_handbook.md`），内容至少含：验证环境绝对路径、python/site-packages 路径、**经实际验证可用的最低 env 组合**（PATH/LD_LIBRARY_PATH/source 脚本、可加载应用栈的验证命令）、训练/评测命令模板、指标提取方法、before_metrics 基线、变更与回退要求、常见错误规避。**启动每个执行验证 subagent 时，必须把手册路径与关键环境命令直接写入任务包/提示**，禁止 subagent 自行探测或反复试错环境；subagent 环境发现与手册不一致时优先信任手册并报告差异，不得无限重试环境安装。`create_execution_task.py` 输出任务包必须携带 `verification_env.env_handbook_path` 字段（`--verify-env-handbook <path>`）。
6. **记录 forward_cmd / reverse_cmd**：执行验证 subagent 在实施变更前，必须先记录 `forward_cmd`（即将执行的变更命令）和 `reverse_cmd`（可独立执行的回退命令），通过 `record-execution` 写入 `workflow_state.json`。`reverse_cmd` 必须满足：可独立执行（不依赖对话上下文）、包含完整环境变量和路径、可直接复制粘贴执行。未记录 forward_cmd/reverse_cmd 的轮次不得实施变更。
7. 执行验证 subagent 实施动作，或在动作超出授权范围时只输出 dry-run / 人工执行建议并标记 `blocked_scope_change_required`。
8. 执行验证 subagent 重新校验目标实例身份、资源约束、基线可比性和测试组网。
9. 执行验证 subagent 执行复测并落盘原始日志。
10. **验证决策（正向 > 1% 保留 / 负向回退）**：执行验证 subagent 计算相对上一轮有效配置的阶段增量收益后，按以下规则决策：

    | 阶段收益 | 决策 | 动作 | 标记 |
    |---------|------|------|------|
    | **> 1%** | **保留（accepted）** | 保留变更，进入下一轮 | `execution_status=accepted` |
    | **≤ 0%** | **回退（rejected）** | 立即执行 `reverse_cmd` 恢复上一轮配置 | `execution_status=rejected`，记录到 `rejected_optimization_actions` |
    | **0% - 1%** | **噪声区（inconclusive）** | 保留变更但记录噪声 | `execution_status=inconclusive` |

    - 负向回退后，后续轮次从上一轮有效配置继续，不得在已回退的配置上叠加新变更。
    - 回退必须验证回退成功（执行 reverse_cmd 后重新校验目标实例身份和基线可比性）。

11. **归档原始数据**：执行验证 subagent 每轮必须归档以下原始数据到 `rounds/round_N_<skill_name>/` 目录：

    | 文件 | 内容 | 必填 |
    |------|------|------|
    | `benchmark_raw.csv` | 压测原始 CSV 输出（含 throughput/TTFT/TPOT 全字段） | 是 |
    | `env_before.json` | 变更前环境变量 + 进程状态快照（PID/线程数/LD_PRELOAD/OMP 等） | 是 |
    | `env_after.json` | 变更后环境变量 + 进程状态快照 | 是 |
    | `forward_cmd.txt` | 本轮实施的完整变更命令 | 是 |
    | `reverse_cmd.txt` | 本轮可独立执行的回退命令 | 是 |
    | `server_log.txt` | 服务启动/运行日志片段（含错误和警告） | 是 |
    | `npu_metrics.json` | NPU 利用率/HBM 带宽采样（AI 推理场景） | AI 推理必填 |
    | `perf_data.bin` | perf record 原始数据（如本轮有采集） | 有采集时必填 |

    - 原始数据归档路径写入 `round_N_summary.json` 的 `raw_data_archive_dir` 字段。
    - 归档文件不得脱敏或截断；脱敏仅在案例归档（`case_archive.json`）阶段执行。

12. 主 agent 读取执行验证 subagent 的 `round_N_summary.json`，更新该 skill 的 `per_skill_iteration_state`。
13. 若该 skill 仍可继续，进入该 skill 下一轮。
14. 若该 skill 触发停止条件，停止该 skill 并进入 `candidate_skill_list` 的下一个 skill。
15. 若所有主优化 skill 均完成、停止或阻塞并说明原因，进入报告、review、环境还原和案例归档。

## 每轮输出要求

每轮至少应记录：

- 当前轮次、当前已生效配置、当前轮候选动作池
- 本轮选中动作、本轮拒绝或暂缓动作
- 本轮主要证据、本轮累计收益
- 执行验证 subagent ID、任务包路径、原始日志路径和回退结果
- `execution_authorization_scope` 校验结果；若触发用户再确认，必须说明超出的具体边界
- 当前 skill 是否继续下一轮、停止原因、候选列表中的下一 skill

## per_skill_gain_summary 字段定义

每个候选和 coverage skill 完成后必须生成收益归因记录，汇总为 `per_skill_gain_summary` 数组：

```json
[
  {
    "skill_name": "cpu-affinity-optimization",
    "execution_order": 1,
    "rounds_attempted": 3,
    "stage_gains_pct": [2.1, 1.3, 0.5],
    "cumulative_gain_pct": 3.9,
    "attribution_method": "single_variable_round",
    "is_isolated": true,
    "confounded_with": [],
    "evidence_paths": ["rounds/round_1_summary.json"],
    "status": "stopped",
    "stop_reason": "all_recommendations_verified"
  }
]
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill_name` | string | 是 | 子 skill 名称，必须与 `candidate_skill_list[].subskill_name` 一致 |
| `execution_order` | integer | 是 | 执行顺序编号，从 1 开始。cpu-affinity-optimization 必须为 1 |
| `rounds_attempted` | integer | 是 | 尝试轮次数 |
| `stage_gains_pct` | number[] | 是 | 每轮相对上一轮阶段收益百分比数组，与 `per_skill_iteration_state.round_gains_pct` 一致 |
| `cumulative_gain_pct` | number/null | 是 | 该 skill 全部轮次的独立累计收益（相对初始基线）；无法计算时为 null |
| `attribution_method` | string | 是 | 归因方法：`single_variable_round`（单变量轮次）/ `baseline_reset`（回基线验证）/ `merged_unresolvable`（合并不可拆分）/ `confounded`（存在混淆因子）/ `coupled_variable_group`（耦合变量组，AI 推理弱耦合协同对合并执行） |
| `is_isolated` | boolean | 是 | 该 skill 的收益是否可独立归因。`false` 时必须填写 `confounded_with` |
| `confounded_with` | string[] | 是 | `is_isolated=false` 时列出混淆的 skill 名称；`is_isolated=true` 时为空数组 |
| `evidence_paths` | string[] | 是 | 各轮轮次摘要路径 |
| `status` | string | 是 | `completed` / `stopped` / `blocked` / `pending` |
| `stop_reason` | string | 否 | 停止原因，仅 `status=stopped` 或 `blocked` 时必填 |

## 优化验证口径

- 所有优化按串行叠加方式验证，每轮基于前一轮已应用配置继续执行
- 每轮必须同时记录阶段增量收益和累计收益
- 报告默认采用串行叠加收益口径；只有用户明确要求时，才额外回到同一基线测单项独立收益
- 不得把多个并发变更后的混合结果拆分为单项收益
- 每轮必须记录分析耗时、实施耗时、验证耗时和总耗时，以及各优化分析项耗时明细
- 每轮必须形成明确的继续/停止决策，停止时必须记录停止原因

- **有效手段继承强制规则（Inheritance of Effective Changes）**：任何被标记 `accepted`（正向 >1% 保留）的手段，无论其变更形态是持久配置还是 **runtime-only**（如 taskset 临时绑核、LD_PRELOAD 挂载、临时环境变量），都必须进入 `effective_config_history` 并在后续所有轮次的验证中**显式重新应用**，否则该手段收益会在后续轮次中丢失、且后续轮次的 before/after 口径被污染为"回退到未叠加状态"。主 agent 在创建后续轮次执行任务包时，必须把 **已生效手段的重新应用命令/脚本**（如 runner 内嵌 CA-02 绑核逻辑）明确写入任务包的 `inherited_active_changes` 字段并随 `forward_cmd` 一并下发；executor 在本轮训练启动时应用全部继承手段，确保 before 基线 = 上一轮有效配置状态。若因实际限制无法继承（如设备和环境绑定、无法重现的临时变更），该轮次收益必须标记 `attribution_degraded=true` 并说明原因，不得静默按独立基线计算。

## cpu-affinity-optimization 执行门控

cpu-affinity-optimization 必须在所有其他候选 skill 之前执行：

- 进入任何其他候选 skill 的执行轮次前，必须确认 `cpu-affinity-optimization` 的 `per_skill_iteration_state.status` 为 `completed` 或 `stopped`。
- 确认方式：主 agent 必须从 `workflow_state.json` 读取 `candidate_skill_list` 中 cpu-affinity 条目的状态，或从 `per_skill_iteration_state` 读取其状态，并调用 `DynamicWorkflowManager.validateCpuAffinityFirst()` 进行验证。
- 若 cpu-affinity 尚未完成，主 agent 禁止加载其他 skill 的执行验证 subagent，禁止生成其他 skill 的执行任务包，禁止进入其他 skill 的迭代轮次。
- 违规执行视为合规失败，最终报告的 `skill_execution_order.cpu_affinity_first_verified` 必须为 `true`。

## 单 Skill 停止规则

架构图要求停止粒度是单个 skill，而不是整个 Agent。

单个 skill 满足以下任一条件时停止该 skill：

- 该 skill 的 high/medium 候选动作全部已验证、拒绝或因安全门禁暂缓。
- 该 skill 所需证据缺失且补采后仍无法形成可验证动作。
- 该 skill 的候选动作全部需要用户未批准的权限、重启、重编译、远程执行或硬件变更。

停止单个 skill 后：

- 必须继续执行 `candidate_skill_list` 中下一个未完成的 skill，包括 coverage 阶段 skill。
- 若所有主优化 skill 都已完成、停止或阻塞并说明原因，输出最终报告。
- 若瓶颈重新分类，必须重新采集或确认 `performance_signal_summary.json`，生成新的 `candidate_skill_list`，不得沿用旧候选列表盲目继续。

`per_skill_iteration_state` 至少记录：

```json
{
  "application-config-optimization": {
    "rounds_attempted": 5,
    "round_gains_pct": [0.6, 0.4, 0.2, 0.0, -0.1],
    "status": "stopped",
    "stop_reason": "all_recommendations_verified",
    "next_candidate_skill": "performance-library-selection"
  }
}
```

## 全局停止规则

全局停止只在以下场景触发：

- `bottleneck_classification=no_active_bottleneck`。
- 瓶颈不可识别，补采后仍为 `unknown_bottleneck`。
- 所有主优化 skill 均已完成、停止或阻塞并说明原因。
- 剩余动作均超出用户批准范围，且没有安全的 dry-run 或人工建议可继续验证。
- 用户要求停止或只输出报告。

## 候选二进制与源码补丁验证口径

当候选动作涉及源码补丁、重编译、替换二进制、替换动态库或运行时注入项时，必须额外执行以下门控：

- 实施前记录当前可执行文件、启动参数、配置文件、动态库、cpuset、NUMA 绑定和回退命令。
- 候选二进制必须先通过版本、健康检查、连接 smoke test、错误日志检查和目标实例身份校验，再进入正式压测。
- 若候选二进制和上一轮已采纳配置不是同一源码基线、同一配置、同一运行库和同一资源约束，只能标记为 `confounded_binary_test`，不得把结果归因为单个补丁。
- 正式验证前应先跑短 warmup。若 warmup 已明显回退、报错或目标实例身份不一致，应立即回退，不进入正式长测。
- 正式压测结果相对上一轮已采纳配置下降超过噪声阈值（默认 `> 2%`）时，必须回退并记录到 `rejected_optimization_actions`。注意：此 `> 2%` 是二进制替换场景的噪声阈值（针对 warmup 后稳态压测）；通用配置类轮次的回退阈值仍按 `≤ 0%` 回退 / `0%-1%` 噪声区 / `> 1%` 保留的三档规则执行。
- 如果代码生成或反汇编验证成功，但 perf 热点和业务指标不匹配，应记录 `codegen_success_but_workload_mismatch=true`，停止围绕该补丁继续叠加优化。

## AI 推理耦合变量分组策略

AI 推理场景（vLLM / SGLang / TGI on Ascend / GPU）中，多个配置变量存在协同或互斥关系，纯单变量原则无法覆盖。本节定义耦合变量分组策略，作为单变量原则的受控扩展。

### 耦合变量组定义

| 耦合类型 | 定义 | 示例（vLLM / NPU 场景） | 归因处理 |
|---------|------|----------------------|---------|
| 强耦合组 | 组内变量互斥或必须同步切换，单变量无法独立验证 | `tensor_parallel_size`(TQE) 与 `async-scheduling` 互斥；`enforce_eager` 与 `graph_mode` 互斥 | 合并执行，标记 `confounded` |
| 弱耦合组 | 组内变量存在协同效应，单变量收益可测量但不完整 | `OMP_NUM_THREADS` + `OMP_WAIT_POLICY`（协同贡献 -68% TTFT）；`gc_threshold` + `max_split_size`（协同 +3.2%） | 组内可拆分，但收益解释需标注协同项 |
| 独立变量 | 与其他变量无耦合，可纯单变量执行 | `tcmalloc` LD_PRELOAD、CPU 绑核、PGO 编译 | 标准单变量轮次 |

### 分组迭代规则

1. **强耦合组**：整组作为一个动作单元在同一轮次合并执行，归因方法标记为 `attribution_method=coupled_variable_group`，`is_isolated=false`，`confounded_with` 列出组内其他变量。
2. **弱耦合组**：组内变量按连续相邻轮次执行，并在该组最后一轮后追加一轮协同复测（不开新变更，仅复测稳态），用于量化协同增量。若协同复测增量 > 1%，将该增量单独标记为 `coupling_synergy_gain_pct`，不归入任一单变量。
3. **独立变量**：按标准单变量原则执行，归因 `single_variable_round`。
4. **组间单变量原则**：一个轮次只允许来自一个耦合组或一个独立变量，组间不得合并。
5. **耦合强度升级**：当不确定耦合强度时，先按弱耦合试做一轮单变量，若单变量收益显著低于预期（< 0.5%）且组内另一变量在历史案例中曾协同生效，升级为弱耦合组并触发协同复测。

### 归因标记扩展

当 `attribution_method=coupled_variable_group` 时，`per_skill_gain_summary` 中必须同时提供以下字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `coupled_variable_group_name` | string | 否 | 耦合组名称，如 `omp_passive_synergy`、`gc_split_synergy`；非耦合组为 null |
| `coupled_variables` | string[] | 否 | 组内变量列表；非耦合组为空数组 |
| `coupling_synergy_gain_pct` | number/null | 否 | 协同复测增量收益；无协同复测时为 null |

## AI 推理预热与快速失败

### 预热要求

vLLM、SGLang 等 AI 推理服务启动后必须执行预热请求才能进入稳态：

- 模型加载完成 ≠ 稳态。首次推理需触发 graph capture、weight 预热、KV cache 池初始化。
- 预热请求集：至少 3-5 个不同长度 prompt，覆盖短/中/长序列，触发 CUDA graph / NPU graph 捕获路径。
- 预热完成后才开始正式压测基线采集；预热请求耗时和成功率必须记录到 `warmup_log`。
- 预热与正式压测之间应有一个冷却窗口（建议 ≥30s），让调度器、缓存池进入稳态。

### 快速失败阈值

以下情况必须立即停止当前轮次并回退，不进入正式压测：

| 快速失败条件 | 处理动作 |
|-------------|---------|
| 模型加载失败 / 权重校验失败 | 立即停止，回退配置，记录 `model_load_failed` |
| 预热请求错误率 >10% | 停止该轮，回退，记录 `warmup_failed` |
| 预热阶段 OOM / KV cache 分配失败 | 停止，回退，记录 `kv_cache_oom` |
| NPU 设备掉卡 / HCCS 链路异常 | 停止，回退，记录 `npu_device_error` |
| 服务健康检查端口未就绪（超时 60s）| 停止，记录 `service_unhealthy` |

### 轮次间最小间隔

每轮变更应用后，必须等待最小间隔再启动下一轮基线采集，避免上一轮变更尾效污染下一轮测量：

| 变更类型 | 最小间隔 | 稳态确认要求 |
|---------|---------|-------------|
| 配置类（`gc_threshold`、`max_split_size` 等）| 60s | tokens/s 稳定 ±2% |
| 调度类（`OMP_NUM_THREADS`、CPU 绑核）| 120s | NPU util 回落 <5%，线程迁移完成 |
| 重启类（`enforce_eager` 切换、二进制替换）| ≥300s | 模型加载 + 预热 + 冷却全部完成 |

间隔期间应记录 `idle_window_seconds` 和稳态确认采样（NPU util、QPS 是否回落到 idle baseline）。

实战参考（Ascend910 + vLLM qwen2.5-1.5b，累计 +94.5%）：

| 轮次 | 变更类型 | 最小间隔 | 稳态确认 |
|------|---------|---------|---------|
| C8 TQE=2 + OMP=8 + PASSIVE | 调度+配置 | 120s | NPU util 回落 <5% |
| gc_threshold=0.95 + max_split_size=50 | 配置 | 60s | tokens/s 稳定 ±2% |
| enforce_eager → graph_mode | 重启类 | 300s | 模型加载 + 预热完成 |
