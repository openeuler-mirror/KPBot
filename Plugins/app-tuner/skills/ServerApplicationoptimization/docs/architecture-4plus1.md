# 服务器应用优化 Agent 4+1 架构设计文档

## 1. 文档目标与权威口径

本文档是 `kpbot-app-tuner` skill 的设计入口，用 4+1 架构视图描述使用场景、模块分层、运行机制、仓库组织和平台装载方式。当前公共行为已按“服务器应用优化 Agent”架构图重构：用户界面输入与确认 → 环境诊断和备份 → 性能采集与瓶颈识别 → 根据采集信息生成候选优化 skill 列表 → 迭代验证 → 报告、review、环境还原和案例归档。

本文档面向两类读者：

- 使用者：理解如何在 `Claude Code`、`Codex`、`Cursor`、`OpenCode` 等编程 Agent 中使用该 skill 组织服务器应用优化流程。
- 维护者：理解主编排逻辑、子 skill 分工、脚本职责、输入输出契约和扩展边界。

权威口径：

- 主实现以 `skills/kpbot-app-tuner/SKILL.md` 为准。
- 架构图映射以 `skills/kpbot-app-tuner/references/application-agent-architecture.md` 为准。
- 细节契约以 `skills/kpbot-app-tuner/references/` 为准。
- 专项能力以 `skills/kpbot-app-tuner/subskills/` 为准。
- `ref-skills/` 只作为仓库内置外部能力源，由统一入口子 skill 条件接入。
## 2. 架构设计原则

- 单一主源：`skills/kpbot-app-tuner/` 是唯一事实来源。
- 自顶向下：先完成测试场景输入、Agent 操作确认、基线确认和环境备份，再进入采集和优化。
- 编排优先：主 skill 负责流程编排、门控、候选 skill 列表、候选池和报告约束，不承接全部专项算法。
- 采集先行：先分类磁盘、网卡、内存、CPU、GPU/NPU 或硬件规格瓶颈，再基于火焰图、热点 so、topdown 和线程信号生成候选 skill 列表。
- 证据驱动：优化建议基于基线、火焰图、热点函数、进程/线程、topdown、系统采样和环境快照。
- 可降级执行：工具缺失、权限不足或外部依赖不可用时显式降级并记录置信度。
- 统一入口：主 skill 不直接路由 `ref-skills/`，只调用框架内 subskill。
- 候选优先：先执行 `candidate_skill_list` 中 `phase=evidence_candidate` 的 skill，再执行 `phase=coverage` 的未命中主优化 skill。
- 单 skill 停止：单个 skill 最多尝试 5 轮，5 轮收益均小于 1% 时停止该 skill，并继续候选列表中的下一个 skill。
- 多平台接入：`.claude/`、`.opencode/`、`.agents/` 是轻量发现入口；Codex、Cursor 等编程 Agent 可直接加载主源目录，逻辑仍回到 `skills/kpbot-app-tuner/`。

## 3. 4+1 视图总览

本 skill 的 4+1 视图映射如下：

- 用例视图：用户如何触发和使用该 skill，支持的调用形式和最终交付物。
- 逻辑视图：主 skill、subskill、ref-skill、references、scripts 如何协作。
- 开发视图：仓库如何组织，维护者如何扩展。
- 运行视图：一次优化任务如何从输入、基线、瓶颈识别、候选 skill 列表、迭代验证到报告闭环。
- 物理视图：不同代理平台如何加载主源，以及运行环境如何约束能力。

```
kpbot-app-tuner 4+1 视图
├── 用例视图
│   ├── 客户端调用方式
│   ├── 整体编排调用
│   ├── 子 skill 独立调用
│   └── 最终输出产物
├── 逻辑视图
│   ├── 主编排 skill
│   ├── 统一入口 subskill
│   ├── 内置 ref-skill
│   ├── references 契约
│   ├── scripts 工具
│   └── 报告交付
├── 开发视图
│   ├── 单一主源
│   ├── 轻量入口
│   ├── 文档与契约
│   └── 扩展边界
├── 运行视图
│   ├── Phase 1 用户输入/基线/环境备份
│   ├── Phase 2 瓶颈识别/证据采集/候选 skill 列表
│   ├── Phase 3 迭代验证/review/环境还原/归档
│   └── 报告生成与自检
└── 物理视图
    ├── Claude Code
    ├── Codex
    ├── Cursor
    ├── OpenCode
    ├── Generic Agent
    └── baremetal / vm / container
```

## 4. 用例视图

用例视图描述用户如何触发和使用该 skill，包括调用方式、使用形式和使用后得到的输出产物。

### 4.1 客户端调用方式

本 skill 面向多种 AI 编程 Agent 使用，各平台的触发方式如下：

| 客户端 | 发现方式 | 触发方式 |
|--------|---------|---------|
| Claude Code | `.claude/skills/` 目录下自动发现 | 对话中直接提及优化意图，如"帮我优化 MySQL 的 CPU 性能" |
| Codex | 直接读取仓库主源或与仓库绑定的 skill 注册 | 在 Codex 会话中引用该 skill |
| Cursor | 通过仓库上下文、规则文件或 Agent 配置指向主源 | 在 Cursor Agent 中引用该 skill 或主源目录 |
| OpenCode | `.opencode/skills/` 目录下自动发现 | 对话中描述优化目标即可 |
| 其他编程 Agent | 支持目录式 `SKILL.md` 或可读取仓库规则 | 指向 `skills/kpbot-app-tuner/` 主源 |

各平台的轻量入口均为薄跳转层，最终加载同一主源 `skills/kpbot-app-tuner/SKILL.md`，行为一致。

### 4.2 使用形式：整体调用与子 skill 独立调用

本 skill 支持两种使用形式：

**形式 A：整体编排调用**

用户发起一次完整的服务器应用优化任务，主编排 skill 自动执行全流程：测试场景输入 → Agent 操作确认 → 环境诊断与备份 → 基线确认 → 性能采集与瓶颈识别 → 根据采集信息生成候选优化 skill 列表 → 串行迭代验证 → 报告、review、环境还原和案例归档。用户只需提供优化意图和必要的环境材料，其余由 skill 编排完成。

**形式 B：子 skill 独立调用**

用户可针对单一优化方向直接调用子 skill，跳过主编排流程：

| 子 skill | 适用场景 |
|----------|---------|
| `cpu-affinity-optimization` | 已知绑核或 NUMA 配置需要优化 |
| `os-optimization` | 排查系统层面 THP、irqbalance、Kernel 参数 |
| `bios-optimization` | 排查 Power Profile、SMT、C-State、NUMA BIOS 配置 |
| `network-optimization` | 网络侧存在瓶颈，需要网卡/中断/协议栈调优 |
| `application-config-optimization` | 应用层线程、连接池、缓存参数需要调整 |
| `compiler-optimization` | 编译选项、LTO、PGO 需要评估和调整 |
| `performance-library-selection` | 替换 malloc、memcpy 等性能库 |
| `accelerator-optimization` | GPU/NPU 等计算卡瓶颈分析 |
| `hardware-upgrade-analysis` | 判断是否触达硬件规格上限 |
| `other-optimization` | 已有 skill 无法覆盖的专项分析 |

独立调用时，用户需自行提供该方向所需的环境证据，子 skill 仅在该领域内进行分析和建议，不执行跨方向编排。

### 4.3 最终输出产物

无论整体调用还是子 skill 独立调用，用户最终获得以下交付物：

- **优化报告** (`final-report.md`)：包含基线/优化后对比、生效配置、每轮收益、瓶颈定位路径和风险回退建议的结构化文档。
- **中间产物**（整体调用时落盘）：`candidate_pool.json`（候选动作池）、`optimization_summary.json`（收益汇总）、`candidate-skill-summary.md`（分析汇总）等，供审计和复现。
- **可执行建议**：每项优化建议均标注实施方式（在线生效/需重启服务/需系统重启），降低误操作风险。

## 5. 逻辑视图

### 5.1 总体模块关系

```
用户输入 (场景 / 目标规格 / 材料 / 变更范围)
  │
  ▼
主编排 skill  SKILL.md
  ├──► references ────────────────────┐
  │    (流程 / 契约 / 检查清单)       │
  ├──► scripts ───────────────────────┤
  │    (采集 / 检查 / 任务包 / 报告)   │
  └──► subskills ─────────────────────┤
       │                              │
       ├── io-memory-network-bottleneck-analysis
       ├── cpu-affinity-optimization ──► ref-skills/cpu-affinity-optimization
       │    └── (不满足时) ──► 内部轻量规则路径
       ├── os-optimization
       ├── bios-optimization
       ├── network-optimization ───────► ref-skills/network-io-performance
       │    └── (不满足时) ──► 内部通用网络路径
       ├── application-config-optimization
       │    └── (按需引用) database-workload-analysis
       ├── compiler-optimization ──► ref-skills/compiler-option-optimization
       │    └── (不满足时) ──► 内部编译选项分析路径
       └── performance-library-selection ──► ref-skills/library-replacement
            └── (不满足时) ──► 内部通用性能库路径
       │                              │
       └──────────────────────────────┘
                    ▼
              最终报告
   (report-template + generate_report.py)
```

### 5.2 分层职责

```
┌── 主编排层 (SKILL.md) ──────────────────────┐
│  主流程阶段、门控、运行纪律、停止条件、报告结构  │
└──┬──────────┬──────────────┬────────────────┘
   │          │              │
   ▼          ▼              ▼
┌────────┐ ┌──────────┐ ┌──────────┐
│门控与  │ │专项分析层 │ │工具脚本层 │
│契约层  │ │subskills │ │scripts   │
│refs    │ └────┬─────┘ └────┬─────┘
└──┬─────┘      │            │
   │      ┌─────▼──────┐     │
   │      │外部能力源   │     │
   │      │ref-skills  │     │
   │      └─────┬──────┘     │
   │            │            │
   └──────┬─────┴──────┬─────┘
          ▼            ▼
   ┌────────────────────────┐
   │  交付层                  │
   │  报告 schema / 模板     │
   │  / generate_report.py  │
   └────────────────────────┘
```

主编排层：

- 定义主流程阶段、门控和运行纪律。
- 维护基线确认、瓶颈识别、候选 skill 列表、候选池、单 skill 停止和全局停止条件。
- 规定最终报告结构和自检要求。

参考资料层：

- `workflow.md`：主流程展开。
- `checklist.md`：阶段检查项。
- `report-schema.md`：报告字段契约。
- `candidate-skill-list.md`、`candidate-skill-analysis.md`、`subagent-orchestration.md`：候选 skill 列表、任务包和 subagent 输出契约。
- `prerequisites.md`、`remote-execution.md`、`optimization-decision-tree.md` 等：依赖、远程执行、优化方向选择和降级规则。

工具脚本层：

- `backup_environment.sh`、`collect_evidence_snapshot.sh`：环境与证据采集入口。
- `detect_bottleneck.sh`、`check_cpu_balance.sh`：轻量检测入口。
- `create_subagent_tasks.py`、`merge_subagent_results.py`：候选 skill 任务包生成和候选池合并。
- `record_timing.py`、`summarize_improvement.py`：耗时与收益汇总。
- `generate_report.py`、`init_report.sh`：报告生成和目录初始化。
- 外部能力检查与包装脚本：`check_external_*`、`run_external_network_io_check.sh`、`install_external_library_replacement.sh`。

交付层：

- 承接原始证据、中间结论、候选动作、串行验证结果、耗时统计、依赖降级和风险回退。
- 最终报告必须自动生成，并通过 Step 9.5 自检。

### 5.3 subskill 与 ref-skill 关系

```
主 skill (只调用框架内 subskill)
  │
  ├──► cpu-affinity-optimization (统一入口)
  │     ├── 路径和依赖满足 ──► ref-skills/cpu-affinity-optimization
  │     │                       (拓扑 / 线程 / IRQ / 策略 / 回滚)
  │     └── 不满足 ──► 内部轻量规则路径
  │
  ├──► network-optimization (统一入口)
  │     ├── 路径和依赖满足 ──► ref-skills/network-io-performance
  │     │                       (接口 / IRQ / 丢包 / 队列)
  │     └── 不满足 ──► 内部通用网络路径
  │
  ├──► performance-library-selection (统一入口)
  │     ├── aarch64 且依赖满足 ──► ref-skills/library-replacement
  │     └── 不满足 ──► 内部通用性能库路径
  │
  └──► application-config-optimization (数据库专项对外入口)
        └── (按需引用) database-workload-analysis (内部数据库分析)
```

统一接入原则：

- 主 skill 只依赖框架内 subskill。
- 统一入口 subskill 决定是否接入 `ref-skills/`。
- `ref-skills/` 不应绕过统一入口直接进入主流程。
- 路径缺失、依赖不足、权限不足时必须记录 fallback reason。

### 5.4 子 skill 职责边界

| 子 skill | 职责 | 特殊边界 |
|---|---|---|
| `io-memory-network-bottleneck-analysis` | 判断网络、磁盘、内存、CPU 等瓶颈并输出统一分类 | 性能采集层，不直接实施优化 |
| `cpu-affinity-optimization` | 绑核、NUMA、内存绑定、线程 CPU 均衡、中断冲突 | 可适配 `ref-skills/cpu-affinity-optimization` |
| `os-optimization` | Kernel、THP、HugePages、irqbalance、sysctl 等建议 | 区分在线、服务重启和系统重启动作 |
| `bios-optimization` | Power Profile、SMT、C-State、NUMA BIOS 配置建议 | 默认只输出人工确认建议 |
| `network-optimization` | 网络瓶颈或次级瓶颈下的网卡、队列、中断、协议栈建议 | 可适配 `ref-skills/network-io-performance` |
| `application-config-optimization` | 线程数、队列、批量、缓存、连接池和数据库型工作负载专项 | 数据库专项唯一对外入口 |
| `database-workload-analysis` | 数据库内部状态分析，MySQL/InnoDB 和 AHI 判断 | 仅供 application-config 按需引用 |
| `compiler-optimization` | 编译器版本、架构选项、LTO、PGO、AutoFDO、向量化 | ARM64 可适配 `ref-skills/compiler-option-optimization` |
| `performance-library-selection` | malloc、memcpy、压缩、加密、校验等性能库选型 | aarch64 可适配 `ref-skills/library-replacement` |
| `accelerator-optimization` | GPU/NPU 利用率、显存/内存、拷贝带宽和设备错误分析 | 无设备时标记 not_present |
| `hardware-upgrade-analysis` | 判断当前规格是否达到容量边界 | 只输出硬件建议，不执行采购或变更 |
| `other-optimization` | 既有 skill 无法覆盖的专项分析 | 默认 analysis_only |

### 5.5 主框架与子 Skill 责任边界

本轮重点验收 `kpbot-app-tuner` 主框架，子 skill 由专项开发者后续迭代。主框架验收范围包括：

- 候选列表和编排：是否严格执行用户确认、环境诊断备份、基线确认、瓶颈识别、性能采集、候选 skill 列表、串行验证、报告、review、还原和归档。
- 接口契约：是否为 subskill 提供稳定的任务包、结果 JSON、候选动作、执行验证、收益统计和报告字段约束。
- 安全门禁：是否默认只读或 dry-run，真实执行是否要求用户批准、`approved-change-id`、验证和回退计划。
- 平台入口：`.claude/`、`.opencode/`、`.agents/` 和主源命名是否一致。

不在主框架本轮验收范围：

- 子 skill 内部专项算法是否最优。
- 子 skill 长文档是否拆分。
- 子 skill 领域细节、案例库、参数库和专家经验补充。

这些问题应作为 `subskills/<name>/SKILL.md` 的专项任务下发给对应开发者；只有当子 skill 缺失、命名不一致、无法被候选列表调用或不满足结果 JSON 契约时，才视为主框架阻塞问题。

## 6. 开发视图

### 6.1 当前目录组织

```text
docs/
  architecture-4plus1.md          # 唯一设计文档：架构、模块关系、运行机制、扩展边界
  usage-guide.md                  # 使用方式、示例命令和平台触发方式
  report-template.md              # 最终报告模板
  context-compaction-solutions.md

ref-skills/                       # 仓库内置外部能力源，由统一入口 subskill 条件接入
  README.md                         # 外部 skill 说明
  SOURCES.md                        # 来源声明
  compiler-option-optimization/     # 来自 KunpengSDK
  cpu-affinity-optimization/
  library-replacement/
  network-io-performance/

skills/kpbot-app-tuner/   # [唯一主源]
  SKILL.md                          # 主编排入口
  agents/openai.yaml
  references/                       # 17 个契约与流程文档
  scripts/                          # 17 个工具脚本
  subskills/                        # 候选列表专项 subskill
    application-config-optimization/
    accelerator-optimization/
    bios-optimization/
    os-optimization/
    hardware-upgrade-analysis/
    other-optimization/
    compiler-optimization/          # 含 patches/ (MySQL LSE patch)
    cpu-affinity-optimization/
    database-workload-analysis/
    io-memory-network-bottleneck-analysis/
    network-optimization/
    performance-library-selection/

.claude/skills/kpbot-app-tuner/SKILL.md   # Claude Code 轻量入口
.opencode/skills/kpbot-app-tuner/SKILL.md # OpenCode 轻量入口
.agents/skills/kpbot-app-tuner/SKILL.md   # 通用 Agent 轻量入口

# 轻量入口职责：发现路径 + 名称一致 + 提示加载主源。轻量包装（非软链接），不复制实现。
# Codex、Cursor 等可直接加载 skills/kpbot-app-tuner/ 主源目录。
# 扩展规则：新增 subskill → subskills/<name>/SKILL.md；新增脚本 → scripts/；
#           新增契约 → references/（同步更新 reading-guide.md）；
#           改动主流程语义 → 同步更新主源 SKILL.md、相关 reference、轻量入口、本文档。
```

### 6.2 中间文件与临时目录

完整编排调用会在输出目录下生成中间产物。默认建议使用 `output/<scenario-name>/` 或用户指定目录，避免与源码混在一起。

| 路径 | 生命周期 | 说明 |
|---|---|---|
| `env/` 或 `environment_backup/` | 保留 | 环境诊断与备份结果，供 review、还原和案例复用 |
| `checkpoints/` | 保留到报告完成 | 阶段检查点，如基线确认、瓶颈识别、候选列表 |
| `evidence/` | 保留 | 统一证据快照，包含静态配置和压测期间动态采集 |
| `candidate-skill-tasks/` | 临时，可归档 | 候选 skill 的任务包 manifest 和单 skill 任务 JSON |
| `candidate-skill-results/` | 保留到报告完成 | subagent 输出的结构化分析、候选动作和验证结果 |
| `execution-tasks/` | 临时，可归档 | 每轮执行验证 subagent 的任务包 |
| `rounds/` | 保留 | 每轮执行验证结果、收益、回退和停止原因 |
| `candidate_pool.json` | 保留 | 合并后的候选动作池和候选 skill 列表 |
| `candidate-skill-summary.md` | 保留 | 候选 skill 分析摘要 |
| `final-report.md` | 最终交付 | 优化报告 |
| `review-result.json` | 保留 | review 结论 |
| `restore-result.json` | 保留 | 环境还原结果或人工待执行项 |
| `case_archive.json` | 长期保留 | 案例归档索引，供后续复用 |

临时目录规则：

- 大输出先落盘，再摘要读取；不要把 `perf report`、`sysctl -a`、完整压测日志直接灌入主上下文。
- `candidate-skill-tasks/` 和 `candidate-skill-results/` 可按案例归档；调试临时文件必须放在 `output/` 或系统临时目录。
- 报告引用中间产物时使用相对路径或脱敏路径。

## 7. 运行视图

### 7.1 当前主流程状态机

```text
[*] 开始
  │
  ▼
Phase 1 用户界面层
  测试场景输入 / Agent 操作确认 / 基线数据确认 / 数据统计口径
  │
  ▼
环境诊断 + 环境备份
  生成 environment_backup_dir 与 restore_baseline_manifest
  │
  ▼
性能采集工具层
  磁盘 / 网卡 / 内存 / CPU / GPU-NPU / 硬件规格
  │
  ▼
瓶颈识别
  │
  ├── unknown_bottleneck ──► 补充采集或输出无法识别瓶颈报告
  ├── no_active_bottleneck ──► 报告 + review + 归档
  └── 有瓶颈
        │
        ▼
      基线确认后深度证据
      火焰图 / 热点函数 / 进程线程 / topdown
        │
        ▼
      candidate_skill_list
      根据采集信息生成候选优化 skill 列表
        │
        ▼
      候选/coverage skill 分析
      生成任务包、subagent 只读分析、合并 candidate_pool
        │
        ▼
      串行迭代验证
      为当前 skill 启动执行验证 subagent
      SelectAction → ApplyAction → Validate → Rollback/Accept
        │
        ├── 单 skill 最多 5 轮且收益均 <1% ──► 停止该 skill，进入下一候选
        ├── 候选已完成 ──► 执行 coverage skill
        └── 所有调优完成或无可识别瓶颈
              │
              ▼
            输出报告
              │
              ▼
            review & 环境还原
              │
              ▼
            数据归档为案例
              │
              ▼
            [*] 结束
```

### 7.2 候选列表数据流

```text
baseline confirmed
  │
  ▼
detect_bottleneck.sh / 采集工具输出 bottleneck_classification
  │
  ▼
collect_evidence_snapshot.sh  统一证据快照
  │
  ▼
performance_signal_summary.json
热点函数 / 热点 so / topdown L1 icache / L3 miss / 线程切换
  │
  ▼
candidate-skill-list.md  生成 candidate_skill_list
  │
  ▼
create_subagent_tasks.py  生成候选 skill 分析任务包
  │
  ▼
分析 subagent 各自读取对应 subskill (只读分析)
  │
  ▼
subagent results JSON  (findings / candidate_actions / timing)
  │
  ▼
merge_subagent_results.py
门控校验 + 合并候选池 + 保留 candidate_skill_list
  │
  ▼
candidate_pool.json
(candidate_actions + optimization_order + candidate_skill_list)
  │
  ▼
串行迭代验证
每轮启动一个执行验证 subagent
读取 candidate action，实施/复测/回退/记录收益
  │
  ▼
generate_report.py  最终报告
```

关键规则：

- 候选 skill 分析阶段只能分析，不得修改系统、重启服务或运行正式收益验证。
- 候选 skill 分析前必须由主 agent 统一采集证据快照，subagent 禁止自行 SSH 采集。
- 主 agent 合并候选池时优先读取 `candidate_pool.json`，避免把 subagent 原始细节回注主上下文。
- 串行迭代验证阶段是唯一可以实施和验证优化动作的阶段；每轮由一个执行验证 subagent 负责当前 skill 的实施、复测、回退和结果落盘。
- 主 agent 不并发执行变更，只负责审批门控、状态流转、收益归因和停止决策。
- 停止粒度优先是单个 skill；停止后必须继续 `candidate_skill_list` 中尚未完成的候选或 coverage skill。

### 7.3 候选池与报告产物

当前运行中间产物会落盘，最终审计结论汇总进报告：

- `candidate-skill-tasks/*.json`：候选 skill 分析任务包。
- `candidate-skill-tasks/manifest.json`：候选 skill 列表计划。
- `candidate-skill-results/*.json`：候选 skill 分析输出和执行验证结果。
- `execution-tasks/*.json`：串行执行验证任务包。
- `candidate_pool.json`：主 agent 使用的候选动作池。
- `candidate-skill-summary.md`：候选 skill 分析汇总。
- `final-report.md`：最终交付报告。
- `review-result.json`、`restore-result.json`、`case_archive.json`：review、还原和归档结果。

### 7.4 停止与收益归因

默认验证口径：

- 每轮基于上一轮已采纳配置继续执行。
- 阶段增量收益相对上一轮有效配置计算。
- 累计收益相对初始干净基线计算。
- 若收益为负、风险超出预期或测试身份不一致，回退该动作。

单 skill 停止条件：

- 同一 skill 已尝试 5 轮，且 5 轮阶段收益均 `< 1%`。
- 当前 skill 的 high/medium 候选动作全部已验证、拒绝或因安全门禁暂缓。
- 当前 skill 所需证据补采后仍缺失，无法形成可验证动作。

全局停止场景：

- `bottleneck_classification=no_active_bottleneck`。
- 补采后仍为 `unknown_bottleneck`。
- 所有候选列表中的主优化 skill 均已完成、停止或阻塞并说明原因。
- 剩余动作均超出用户批准范围。
- 用户要求停止或只输出报告。

## 8. 物理视图

### 8.1 平台入口

```
仓库 (Repository)
  │
  │
  ├── Claude 入口:   .claude/skills/kpbot-app-tuner/SKILL.md
  │     ├── ► Claude Code (发现加载)
  │     └── (薄跳转) ──► 主源
  │
  ├── OpenCode 入口: .opencode/skills/kpbot-app-tuner/SKILL.md
  │     ├── ► OpenCode (发现加载)
  │     └── (薄跳转) ──► 主源
  │
  ├── Agent 入口:    .agents/skills/kpbot-app-tuner/SKILL.md
  │     ├── ► Generic Agent (发现加载)
  │     └── (薄跳转) ──► 主源
  │
  └── 直接主源:      skills/kpbot-app-tuner/
        ├── ► Codex / Cursor / 其他编程 Agent
        └── 读取 SKILL.md、references/、subskills/、scripts/
```

### 8.2 运行环境能力边界

```
运行环境                          本地工具
  │                                │
  ├── baremetal                    ├── perf / BPF / topdown
  │   (完整拓扑与中断可见性)         │
  │                                ├── mpstat / pidstat / iostat / sar
  ├── vm                           │
  │   (拓扑和中断可能降级)           ├── ss / ethtool / ip
  │                                │
  └── container                    └── readelf / ldd / gcc
      (受 cpuset/cgroup/权限约束)
  │                                │
  └────────────┬───────────────────┘
               ▼
       分析能力与建议置信度
```

环境影响：

- 物理机：可执行最完整分析路径。
- 虚拟机：需注意 NUMA、IRQ、PMU 和拓扑信息是否真实暴露。
- 容器：绑核限制在 cpuset 内，IRQ/NUMA/系统级调优建议必须保守化。

依赖影响：

- 缺少 `perf` 会降低热点和 topdown 置信度。
- 缺少 `pidstat`、`mpstat` 会降低线程与 CPU 均衡性判断能力。
- 缺少 `iostat`、`sar`、`ethtool` 会降低非 CPU 瓶颈和网络分析能力。
- 缺少 `readelf`、`ldd`、编译器信息会降低编译与性能库判断能力。

## 9. 横切关注点

### 依赖检查与降级

所有子流程都必须显式记录依赖状态。缺失依赖时输出：

- 缺失工具或权限。
- 跳过或降级的分析项。
- fallback 路径。
- 当前结论置信度。

### 目标实例身份校验

多实例场景允许存在多个同名进程，但本次压测目标实例必须唯一可识别。后续优化前至少确认端口、PID、可执行文件、启动参数、配置文件、运行时库和注入项。

### 容器感知

容器环境下：

- CPU 亲和性只在 cpuset 子集内建议。
- IRQ 和跨 NUMA 建议保守输出。
- `taskset`、`numactl` 失败时优先判断是否为容器边界。

### 数据库专项与 AHI 决策

数据库型工作负载由 `application-config-optimization` 统一承接。MySQL/InnoDB 场景中，AHI 是场景化决策项，不只是采集项。

### ARM/aarch64 编译与性能库协同

aarch64 场景应联合判断：

- GCC 版本和后端优化能力。
- `-mcpu=native` 展开是否符合 CPU 特性。
- LSE、NEON/SVE、PGO/AutoFDO 适用性。
- 外部 `library-replacement` 是否可用。

### 报告标准化

最终报告必须包含：

- 环境和目标规格。
- 基线和目标实例身份校验证据。
- 瓶颈定位路径。
- 候选 skill 分析记录和候选池摘要。
- 每轮生效配置、新增动作、阶段收益、累计收益。
- 继续/停止判定与原因。
- 依赖状态、降级说明、风险和回退建议。
- `optimization_timing` 与 `optimization_timing_details`。

## 10. 演进方向

- 增强脚本的真实采集深度和结果解析能力。
- 继续拆分过长 reference，保持渐进加载。
- 为更多数据库和应用类型增加专项模板。
- 增强 `generate_report.py` 与 report schema 的结构化报告能力。
- 为轻量入口增加自动同步或一致性检查脚本。
- 继续标准化外部 skill 的接入协议和 fallback 输出。

## 11. 结论

`kpbot-app-tuner` 当前架构是“主编排 skill + references 契约 + 多专项 subskill + 仓库内置 ref-skill + 工具脚本 + 标准报告”的可扩展优化编排框架。

从 4+1 视角看：

- 用例视图覆盖客户端调用方式、整体编排调用、子 skill 独立调用和最终交付物。
- 逻辑视图明确主 skill、subskill、ref-skill、references、scripts 和报告边界。
- 开发视图以单一主源和轻量轻量入口降低维护成本。
- 运行视图以用户确认、环境备份、瓶颈识别、候选 skill 列表、迭代验证、review、还原和归档形成证据闭环。
- 物理视图明确平台加载、环境约束和依赖降级对分析能力的影响。
