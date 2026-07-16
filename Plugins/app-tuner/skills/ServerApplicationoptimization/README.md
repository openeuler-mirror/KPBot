# Server Application Optimization

一个面向服务器应用优化工程师的 skill 框架仓库，用于将服务器应用性能优化流程沉淀为可被 `Claude Code`、`Codex`、`Cursor`、`OpenCode` 和其他编程 Agent 加载和复用的技能结构。

当前版本提供的是“框架 + 文档 + 子 skill 骨架 + 占位脚本”，重点解决以下问题：

- 如何把服务器应用优化流程标准化
- 如何拆分主 skill 与子 skill
- 如何支持多种 agent 工具的 skill 加载方式
- 如何沉淀基线测试、瓶颈分析、优化动作与最终报告模板

## 项目目标

本项目围绕“服务器应用优化”构建统一工作流，覆盖以下阶段：

1. 完成启动输入询问、测试场景输入、Agent 操作确认和基线数据确认
2. 诊断并备份当前环境软硬件关键信息
3. 识别磁盘、网卡、内存、CPU、GPU/NPU 和硬件规格瓶颈
4. 根据瓶颈动态路由到对应 skill
5. 通过执行验证 subagent 串行复测优化动作
6. 输出报告、review、环境还原和案例归档

## 当前范围

当前仓库已实现：

- 主 skill 框架
- 动态路由子 skill 骨架
- 文档目录 `docs/`
- 报告模板
- 环境备份、证据快照、报告初始化、占位测试、收益汇总和报告生成脚本
- 仓库内置 `library-replacement` 与 `network-io-performance` subskill
- 外部能力的检测、接入与 fallback 脚本
- Claude Code、Codex、Cursor、OpenCode、通用 Agent 多平台接入；Codex/Cursor 可直接读取主源目录

当前暂未实现：

- 深度自动化网络、磁盘、内存带宽瓶颈识别逻辑
- 真实 BIOS/OS 参数调优执行
- 真实 benchmark 自动部署和执行
- 真实 perf/BPF/topdown 数据采集解析
- 深度数据库状态自动解析

## 目录结构

```text
.
├── README.md
├── docs/
│   ├── architecture-4plus1.md
│   ├── usage-guide.md
│   └── report-template.md
├── ref-skills/
│   ├── README.md
│   ├── SOURCES.md
│   ├── cpu-affinity-optimization/
│   ├── library-replacement/
│   └── network-io-performance/
├── skills/
│   └── server-application-optimization/
│       ├── SKILL.md
│       ├── agents/openai.yaml
│       ├── references/
│       ├── scripts/
│       └── subskills/
├── .claude/skills/server-application-optimization/
├── .opencode/skills/server-application-optimization/
└── .agents/skills/server-application-optimization/
```

## 核心 Skill

主 skill 位于：

- `skills/server-application-optimization/SKILL.md`

它负责整体编排，而不是直接承载全部实现逻辑。当前定义的核心流程为：

0. 执行前准备：询问场景/环境信息是否已提供、选择基线优先或已运行应用入口
1. 环境备份：备份前询问是否采集 BIOS 配置；如需采集，需提供 BMC/IPMI/Redfish 账号和密码/token
2. 场景准备
3. 目标规格约束
4. 基线测试
5. 基线确认：向用户确认基线数据和测试用例信息
6. 非 CPU 瓶颈排查
基线后深度证据采集
动态 skill 分析
门控校验与候选池合并
串行迭代验证（执行验证 subagent）
报告输出
review、环境还原和案例归档

## 子 Skill 列表

当前已拆分以下子 skill：

- `bios-optimization`
- `os-optimization`
- `network-optimization`
- `cpu-affinity-optimization`
- `performance-library-selection`
- `compiler-optimization`
- `application-config-optimization`
- `accelerator-optimization`
- `hardware-upgrade-analysis`
- `other-optimization`
- `io-memory-network-bottleneck-analysis`
- `database-workload-analysis`

其中 `io-memory-network-bottleneck-analysis` 已实现轻量多指标瓶颈预筛规则（磁盘、网络、内存带宽判定）和配套脚本。`database-workload-analysis` 当前提供数据库通用分析框架和 MySQL/InnoDB 示例。

数据库型工作负载专项对外由 `application-config-optimization` 统一承接；`database-workload-analysis` 保留在 `subskills/` 中，供其按需引用。

## 多平台接入方式

本仓库采用“单一主源 + 轻量入口”的组织方式：

- 主源：`skills/server-application-optimization/`
- Claude Code 入口：`.claude/skills/server-application-optimization/`
- OpenCode 入口：`.opencode/skills/server-application-optimization/`
- 通用 Agent 入口：`.agents/skills/server-application-optimization/`
- Codex / Cursor：直接读取 `skills/server-application-optimization/` 主源目录

维护原则是以主源为准，轻量入口只做发现和跳转。

## 文档说明

- [docs/architecture-4plus1.md](docs/architecture-4plus1.md)
  唯一设计文档，说明 4+1 架构、主 skill 编排、子 skill/ref-skill 关系、运行闭环和平台入口
- [docs/usage-guide.md](docs/usage-guide.md)
  使用指南，说明不同平台如何加载和使用该 skill
- [docs/report-template.md](docs/report-template.md)
  首版优化报告模板

## 脚本说明

脚本位于 `skills/server-application-optimization/scripts/`：

- `backup_environment.sh`
  按单条命令顺序执行环境采集并落盘
- `init_report.sh`
  初始化报告工作目录
- `run_placeholder_benchmark.sh`
  预留 benchmark 挂接入口
- `create_subagent_tasks.py`
  生成动态 skill 分析任务包
- `merge_subagent_results.py`
  校验并合并 subagent JSON 输出为候选池
- `create_execution_task.py`
  生成单轮串行执行验证 subagent 任务包
- `record_timing.py`
  统一记录分析、实施、验证和总耗时
- `collect_mysql_status.sh`
  采集 MySQL `SHOW VARIABLES`、`SHOW GLOBAL STATUS` 和 InnoDB 状态
- `apply_optimization_action.sh`
  受控执行调优动作，默认 dry-run，显式 `--execute` 才修改系统
- `collect_evidence_snapshot.sh`
  在动态 skill 分析前统一采集压测运行期动态证据和静态证据
- `generate_report.py`
  根据结构化输入自动生成最终报告
- `summarize_improvement.py`
  汇总串行叠加验证下的累计收益摘要
- `check_external_library_replacement.sh`
  检测仓库内或经审查 fallback 的 `library-replacement` 是否可用
- `install_external_library_replacement.sh`
  离线优先的显式安装辅助脚本；外部 clone 必须传 `--allow-clone`
- `check_external_network_io_skill.sh`
  检测仓库内或经审查 fallback 的 `network-io-performance` 是否可用
- `run_external_network_io_check.sh`
  通过当前框架包装调用外部网络专项检查脚本

## 快速开始

### 1. 查看主 skill

从以下文件开始阅读：

- `skills/server-application-optimization/SKILL.md`

### 2. 初始化环境备份目录

```bash
skills/server-application-optimization/scripts/backup_environment.sh ./output/env
```

### 3. 初始化报告目录

```bash
skills/server-application-optimization/scripts/init_report.sh ./output/report demo-scenario
```

### 4. 运行占位 benchmark

```bash
skills/server-application-optimization/scripts/run_placeholder_benchmark.sh demo-scenario
```

### 5. 采集 MySQL 状态

```bash
skills/server-application-optimization/scripts/collect_mysql_status.sh \
  --output-dir ./output/mysql -- --defaults-extra-file=/path/to/my.cnf
```

### 6. 生成并合并动态 skill 任务

```bash
skills/server-application-optimization/scripts/create_subagent_tasks.py \
  --scenario demo-scenario \
  --baseline ./output/baseline.json \
  --evidence-dir ./output/evidence \
  --target-pid <pid> \
  --output-dir ./output/dynamic-skill-tasks
```

subagent 输出 JSON 后合并候选池：

```bash
skills/server-application-optimization/scripts/merge_subagent_results.py \
  --results-dir ./output/dynamic-skill-results \
  --output-candidate-pool ./output/candidate_pool.json \
  --output-summary ./output/route-summary.md
```

### 7. 汇总优化收益

```bash
python3 skills/server-application-optimization/scripts/summarize_improvement.py \
  --baseline baseline.json \
  --candidate tuned.json \
  --round-name round-2
```

### 8. 检查外部 `library-replacement`

```bash
skills/server-application-optimization/scripts/check_external_library_replacement.sh
```

### 9. 显式执行外部 `library-replacement` 安装辅助

```bash
skills/server-application-optimization/scripts/install_external_library_replacement.sh
```

## 报告输出

首版报告模板已经提供，报告建议至少包含：

- 项目背景
- 环境信息备份
- 测试场景与部署说明
- 基线数据
- 瓶颈定位过程
- 数据库型工作负载的应用配置专项分析
- 优化动作清单
- 分阶段累计验证结果
- 优化动作耗时统计
- 最终综合收益
- 风险与回退建议
- 待补充项

## 外部依赖接入

当前框架默认内置两套外部来源能力：

- `ref-skills/library-replacement`
- `ref-skills/network-io-performance`

当前加载策略：

- 默认优先从仓库根目录 `ref-skills/` 查找并使用
- 若仓库内 subskill 依赖不满足，框架会显式降级并记录 `fallback_reason`，回退到内部通用路径
- 不要求用户手动指定外部路径

## 后续演进方向

- 实现非 CPU 瓶颈分析子 skill 的真实逻辑
- 为 BIOS/OS、绑核、编译器、性能库和应用配置子 skill 增加规则库
- 增加真实 perf/BPF/topdown 数据解析
- 增加报告结构化输出能力
- 增加轻量入口自动同步脚本

## License

当前仓库未单独声明许可证。如需开源发布，建议后续补充明确的 License 文件。
