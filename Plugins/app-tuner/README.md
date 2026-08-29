# app-tuner

应用级调优插件 — 服务器应用性能优化的端到端 Agent 工作流。

同时支持 **Claude Code** 和 **OpenCode** 两种 AI 编码工具。

## 概述

app-tuner 提供 1 个主编排 skill(`kpbot-app-tuner`)及其内部资源:

- **主 skill**: `skills/kpbot-app-tuner/` — 编排器,负责环境采集→瓶颈识别→动态路由→迭代验证→报告输出
- **分析型子 skill**(subskills,自研,12 个): BIOS/OS/网络/CPU 亲和性/编译/性能库/应用配置/加速卡/硬件升级/瓶颈预筛/数据库专项/其他兜底
- **质量保障**: 对抗性审查、current_run 门控、subagent 独立上下文执行

## 安装

### OpenCode

```bash
# 克隆 KPBot 仓库
git clone https://atomgit.com/openeuler/KPBot.git

# 安装到你的项目
cd /path/to/your-app-project
/path/to/KPBot/install.sh project opencode

# 启动
opencode
```

安装后 `install.sh` 会自动:
1. 将 skill 拷贝到 `.opencode/skills/`
2. 生成 `AGENTS.md` 系统提示词(若仓库根有 CLAUDE.md)

验证安装:

```bash
opencode debug skill | grep app-tuner
```

### Claude Code

```bash
# 安装 marketplace
claude plugins marketplace add https://atomgit.com/openeuler/KPBot.git

# 或本地安装
claude plugins install --plugin-dir /path/to/KPBot/Plugins/app-tuner
```

## 技能组成

### 主 skill(编排器)

| 技能 | 描述 |
|------|------|
| `kpbot-app-tuner` | 服务器应用优化编排器 — 环境采集、瓶颈路由、subagent 迭代验证、报告输出 |

### 分析型子 skill(subskills,自研)

| 技能 | 描述 |
|------|------|
| `bios-optimization` | BIOS 优化建议(SMT/C-State/NUMA/内存通道) |
| `os-optimization` | OS 层优化(governor/THP/HugePages/sysctl/ulimit) |
| `network-optimization` | 网络参数调优(网卡/IRQ/队列/RSS/RPS) |
| `cpu-affinity-optimization` | CPU 亲和性(绑核/绑内存/中断亲和) |
| `performance-library-selection` | 性能库选型(malloc/memcpy/压缩/加密) |
| `compiler-optimization` | 编译优化(版本/架构参数/LTO/PGO/向量化) |
| `application-config-optimization` | 应用配置(线程/队列/批量/缓存/连接) |
| `accelerator-optimization` | GPU/NPU 加速卡利用率分析 |
| `hardware-upgrade-analysis` | 硬件规格升级建议 |
| `io-memory-network-bottleneck-analysis` | 非 CPU 瓶颈预筛 |
| `database-workload-analysis` | 数据库专项分析(MySQL/InnoDB) |
| `other-optimization` | 其他优化方向兜底 |

## 目录结构

```
app-tuner/
├── .claude-plugin/plugin.json         # 插件清单
├── README.md                          # 本文件
├── docs/                              # 设计文档与使用指南
│   ├── architecture-4plus1.md
│   ├── usage-guide.md
│   ├── report-template.md
│   └── ...
├── examples/                          # 示例运行产物
│   └── mysql-test/
├── skills/                            # 技能目录(扁平)
│   └── kpbot-app-tuner/               # 主 skill
│       ├── SKILL.md                   # 唯一规范源
│       ├── agents/                    # 平台 agent 配置
│       ├── references/                # 参考文档(22 个)
│       ├── scripts/                   # 脚本(22 个)
│       └── subskills/                 # 自研分析型子 skill(12 个，含 scripts/ 和 references/)
└── opencode/                          # OpenCode 覆盖层(差异文件)
```

## 使用指南

### 快速上手

安装后直接在 Claude Code 或 OpenCode 中用自然语言描述你的优化需求:

```
帮我优化这个 MySQL 服务的性能,目标是提升 sysbench read-only QPS
```

```
分析这个应用的瓶颈,环境信息已经采集在 /path/to/env-backup
```

### 主要入口

| 场景 | 告诉 AI | 底层技能 |
|------|---------|---------|
| 完整优化流程 | "帮我优化这个服务器应用的性能" | `kpbot-app-tuner` |
| 环境备份 | "备份当前环境信息" | `scripts/backup_environment.sh` |
| 报告生成 | "生成优化报告" | `scripts/generate_report.py` |

### 优化工作流

```
环境采集 → 基线测试 → 瓶颈识别 → 动态路由 subskill → subagent 迭代验证 → 报告输出 → 环境还原
```

## 文档

- [架构设计(4+1)](docs/architecture-4plus1.md) — 主 skill 编排、子 skill 关系、运行闭环
- [使用指南](docs/usage-guide.md) — 不同平台如何加载和使用
- [报告模板](docs/report-template.md) — 首版优化报告模板

## 脚本说明

脚本位于 `skills/kpbot-app-tuner/scripts/`,主要包括:

| 脚本 | 作用 |
|------|------|
| `backup_environment.sh` | 环境信息采集 |
| `collect_evidence_snapshot.sh` | 动态证据快照采集 |
| `detect_bottleneck.sh` | 瓶颈识别 |
| `create_subagent_tasks.py` | 生成 subagent 任务包 |
| `merge_subagent_results.py` | 合并候选池 |
| `create_execution_task.py` | 生成执行验证任务 |
| `apply_optimization_action.sh` | 受控执行调优动作 |
| `generate_report.py` | 生成最终报告 |
| `dynamic_workflow_manager.js` | 工作流状态管理 |
| `record_timing.py` | 耗时记录 |
| `summarize_improvement.py` | 收益汇总 |

## License

Apache-2.0
