# code-optimizer

鲲鹏/ARM 代码优化插件 — 自动化 C/C++ 性能分析与优化 Pipeline。

同时支持 **Claude Code** 和 **OpenCode** 两种 AI 编码工具。

## 概述

code-optimizer 提供 36 个专业技能（skills），覆盖从热点分析到代码优化的完整 Pipeline：

- **性能分析**: 热点分析、火焰图解析、Top-down 分析、SPE 分析、调用上下文分析
- **优化决策**: 意图解析、任务分解、优化策略决策、架构分析
- **代码优化**: 向量化、汇编优化、循环展开、分支消除、操作融合、预取优化、访存优化
- **平台支持**: Kunpeng-0xd01/0xd03/0xd06 微架构深度优化
- **ARM 指令**: 完整的 ARM 指令集查询（Base/SVE/NEON/SME，共 1527+ 条指令）
- **质量保障**: 对抗性审查、优化验证、修复代码

## 安装

### OpenCode

```bash
# 克隆 KPBot 仓库
git clone https://gitcode.com/KPBot/KPBot.git

# 安装到你的项目
cd /path/to/your-c-project
/path/to/KPBot/init.sh project opencode

# 启动
opencode
```

安装后 `init.sh` 会自动：
1. 将 36 个技能软链接到 `.opencode/skills/`
2. 生成 `AGENTS.md` 系统提示词

验证安装：

```bash
opencode debug skill | grep optimize   # 确认 skills 已注册
```

### Claude Code

```bash
# 安装 marketplace
claude plugins marketplace add https://gitcode.com/KPBot/KPBot.git

# 或本地安装
claude plugins install --plugin-dir /path/to/KPBot/Plugins/code-optimizer
```

安装后，Claude Code 会根据上下文自动激活相关 skills。

## 技能分类

### 分析类 (Analysis)
| 技能 | 描述 |
|------|------|
| `analyze-hotspot` | 热点函数分析 |
| `analyze-architecture` | 代码架构分析 |
| `analyze-caller-context` | 调用上下文分析 |
| `analyze-testcase` | 测试用例分析 |
| `arm-spe-analysis` | ARM SPE (Statistical Profiling Extension) 分析 |
| `register-pressure-analysis` | 寄存器压力分析 |

### 优化类 (Optimization)
| 技能 | 描述 |
|------|------|
| `apply-optimization` | 应用优化方案 |
| `apply-generic-optimization` | 通用优化应用 |
| `apply-vectorization` | 向量化优化（NEON/SVE） |
| `asm-optimization` | 汇编级优化 |
| `branch-elimination` | 分支消除 |
| `loop-unrolling` | 循环展开 |
| `operation-fusion` | 操作融合 |
| `prefetch-optimization` | 预取优化 |
| `memory-access-optimization` | 访存优化 |
| `scalar-vector-hybrid` | 标量-向量混合优化 |
| `source-transform-autovec` | 源码变换辅助自动向量化 |
| `precision-transform` | 精度变换优化 |
| `special-case-optimization` | 特例优化 |
| `compiler-flag-tuning` | 编译选项调优 |

### Pipeline 类 (Workflow)
| 技能 | 描述 |
|------|------|
| `optimize-pipeline` | 优化 Pipeline 编排 |
| `optimize-pipeline-workflow` | 优化 Pipeline 工作流 |
| `drive-claude-optimize-pipeline` | 驱动 Claude 执行优化 Pipeline |
| `batch-drive-optimize-pipeline` | 批量驱动优化 Pipeline |
| `interactive-optimizer` | 交互式优化器 |
| `parse-intent` | 意图解析 |
| `decompose-tasks` | 任务分解 |
| `decide-optimization` | 优化策略决策 |
| `gather-context` | 上下文收集 |
| `prepare-project` | 项目准备 |

### 平台知识 (Platform)
| 技能 | 描述 |
|------|------|
| `kunpeng_microarch` | Kunpeng-0xd01/0xd03/0xd06 微架构知识库 |
| `arm-instructions-query` | ARM 指令集查询工具 |

### 质量保障 (Quality)
| 技能 | 描述 |
|------|------|
| `verify-optimization` | 优化验证 |
| `adversarial-review` | 对抗性审查 |
| `fix-code` | 代码修复 |
| `evolve-skill` | 技能演化 |

## 使用方式

### 在 OpenCode 中

安装后直接在 OpenCode 中用自然语言描述优化需求：

```
> 帮我优化 src/math/matrix.c 中的 matmul 函数，目标平台 Kunpeng-0xd01
```

```
> 用 NEON 指令向量化这个求和循环
```

```
> 查询 vmlaq_f32 和 vfmaq_f32 的区别
```

主要入口（自然语言描述需求，底层自动调用对应技能）：

| 场景 | 告诉 OpenCode | 底层技能 |
|------|-------------|---------|
| 完整优化流程 | "帮我优化这个项目的性能" | `optimize-pipeline` |
| 逐函数交互优化 | "逐函数分析并优化这个模块" | `interactive-optimizer` |
| 查 ARM 指令 | "查询 vaddq_f32 的用法" | `arm-instructions-query` |
| SPE 采样分析 | "用 ARM SPE 分析缓存 miss" | `arm-spe-analysis` |

### 在 Claude Code 中

安装后，Claude Code 会根据上下文自动激活相关 skills。

## 支持平台

- Kunpeng-0xd01
- Kunpeng-0xd03
- Kunpeng-0xd06
- 通用 AArch64 平台

## License

[Apache-2.0](../../LICENSE)
