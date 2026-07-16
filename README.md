# KPBot

面向鲲鹏/ARM 生态的高性能计算工具集，为 Claude Code 和 OpenCode 提供专业的性能分析与优化插件。

## 插件列表

| 插件 | 类别 | 描述 |
|------|------|------|
| **[code-optimizer](Plugins/code-optimizer/)** | performance | 鲲鹏/ARM 代码优化 — 自动化 C/C++ 性能分析与优化 Pipeline（36 skills） |
| **[app-tuner](Plugins/app-tuner/)** | performance | 应用级调优 — 针对具体应用场景的深度性能调优 |

### 即将推出

| 插件 | 类别 | 描述 |
|------|------|------|
| **system-profiler** | analysis | 系统 profiling — 系统级性能分析与瓶颈定位 |
| **x86-to-arm** | migration | x86 到 ARM 平台迁移 — 代码移植、指令映射、兼容性分析 |

---

## 安装

KPBot 提供统一的安装脚本 `install.sh`，同时支持 Claude Code 和 OpenCode。

### 前置条件

```bash
git clone https://atomgit.com/openEuler/KPBot.git
# 已 clone 的仓库：cd KPBot && git pull
```

### 用法

```bash
install.sh [level] [tool] [install_path]
```

| 参数 | 可选值 | 默认值 | 说明 |
|------|--------|--------|------|
| `level` | `project` / `global` | `project` | 安装级别：项目级或全局级 |
| `tool` | `claude` / `opencode` | `claude` | 目标 AI 编码工具 |
| `install_path` | 任意路径 | 当前目录 | 项目级安装的目标路径 |

### 安装示例

```bash
# 项目级安装（Claude Code，当前目录）
/path/to/KPBot/install.sh

# 项目级安装（OpenCode，指定路径）
cd /path/to/your-c-project
/path/to/KPBot/install.sh project opencode

# 全局安装（Claude Code）
/path/to/KPBot/install.sh global claude

# 全局安装（OpenCode）
/path/to/KPBot/install.sh global opencode
```

### 安装路径

| 工具 | 项目级 | 全局 |
|------|--------|------|
| Claude Code | `.claude/skills/` + `CLAUDE.md` | `~/.claude/skills/` + `~/.claude/CLAUDE.md` |
| OpenCode | `.opencode/skills/` + `AGENTS.md` | `~/.config/opencode/skills/` + `AGENTS.md` |

### 安装脚本自动完成

1. **Skills 拷贝** — 将插件技能拷贝到目标工具的 skills 目录
   - Claude Code：拷贝全部 36 个 skills（claude 格式）
   - OpenCode：先拷贝全部 skills 作基底，再用 `opencode/` 覆盖层替换 26 个差异文件为 opencode 格式，并跳过 2 个 claude-only skill（共 34 个）
2. **配置文件** — 当仓库根目录存在 `CLAUDE.md` 时生成 `CLAUDE.md`（Claude Code）或 `AGENTS.md`（OpenCode）；不存在则跳过
3. **冲突处理** — 已有文件时支持覆盖/合并/跳过（全局模式交互选择）
4. **健康检查** — 验证安装完整性并生成 `kpbot-manifest.json`

### 验证安装

```bash
# OpenCode
opencode debug skill | grep optimize   # 确认 skills 已注册

# Claude Code
# 启动 claude 后，skills 会根据上下文自动激活
```

---

## 使用指南

### 快速上手：一句话优化

安装后直接在 Claude Code 或 OpenCode 中用自然语言描述你的优化需求：

```
帮我优化 matrix_multiply 函数，目标平台是 Kunpeng-0xd01
```

```
分析这个热点函数的性能瓶颈，给出优化建议
```

```
用 NEON 指令向量化这个循环
```

### 主要入口

| 场景 | 告诉 AI | 底层技能 |
|------|---------|---------|
| 完整优化流程 | "帮我优化这个项目的性能" | `optimize-pipeline` |
| 逐函数交互优化 | "逐函数分析并优化这个模块" | `interactive-optimizer` |
| 查 ARM 指令 | "查询 vaddq_f32 的用法" | `arm-instructions-query` |
| SPE 采样分析 | "用 ARM SPE 分析缓存 miss" | `arm-spe-analysis` |

### 优化 Pipeline 流程

完整优化 Pipeline 分 6 个阶段自动执行：

```
准备阶段          分析阶段          决策阶段          执行阶段          验证阶段          审查阶段
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ gather-     │  │ analyze-    │  │ decide-     │  │ apply-      │  │ verify-     │  │ adversarial │
│ context     │→ │ hotspot     │→ │ optimization│→ │ optimization│→ │ optimization│→ │ review      │
│ parse-intent│  │ analyze-    │  │             │  │ (向量化/    │  │ (编译/测试/ │  │ (挑战所有   │
│ prepare-    │  │ caller      │  │             │  │  循环展开/  │  │  性能对比)  │  │  声明)      │
│ project     │  │             │  │             │  │  预取/...)  │  │             │  │             │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘
```

每个阶段对应一个独立的 skill，由 LLM 按序调用。

### 可用技能列表

Claude Code 安装全部 **36 个** skill；OpenCode 安装 **34 个**（跳过 2 个 claude-only）。

<details>
<summary>点击展开完整列表</summary>

**分析类**
- `analyze-hotspot` — 热点函数动态+静态分析
- `analyze-architecture` — 代码仓库架构分析
- `analyze-caller-context` — 调用上下文 14 维度分析
- `analyze-testcase` — 测试用例性能画像分析
- `arm-spe-analysis` — ARM SPE 采样分析
- `register-pressure-analysis` — 寄存器压力诊断

**优化类**
- `apply-vectorization` — NEON/SVE/SME 向量化
- `apply-optimization` — 优化策略路由执行
- `apply-generic-optimization` — 通用优化（兜底）
- `asm-optimization` — 汇编级优化（LDP/STP/后索引等）
- `loop-unrolling` — 循环展开
- `branch-elimination` — 分支消除
- `operation-fusion` — 操作融合
- `prefetch-optimization` — 软件预取
- `memory-access-optimization` — 访存模式优化（AoS→SoA 等）
- `scalar-vector-hybrid` — 标量/矢量混合决策
- `source-transform-autovec` — 源码变形辅助自动向量化
- `precision-transform` — 精度变换
- `special-case-optimization` — 特例快路径
- `compiler-flag-tuning` — 编译选项调优

**Pipeline/编排类**
- `optimize-pipeline` — 完整优化 Pipeline（Subagent 编排）
- `optimize-pipeline-workflow` — 完整 Pipeline（Workflow 工具版）
- `interactive-optimizer` — 交互式逐函数优化
- `gather-context` — 上下文收集
- `parse-intent` — 意图解析
- `prepare-project` — 项目准备与基线建立
- `decompose-tasks` — 任务分解与排序
- `decide-optimization` — 优化策略门控
- `drive-claude-optimize-pipeline` — 驱动 Claude 执行 Pipeline _(claude-only)_
- `batch-drive-optimize-pipeline` — 批量自动化评估 _(claude-only)_

**平台知识**
- `kunpeng_microarch` — Kunpeng-0xd01/0xd03/0xd06 微架构知识库
- `arm-instructions-query` — ARM 指令集查询（1527+ 条）

**质量保障**
- `verify-optimization` — 编译+测试+性能验证
- `adversarial-review` — 对抗性审核
- `fix-code` — 编译/测试错误修复
- `evolve-skill` — 技能持续进化

</details>

### 常用操作示例

#### 1. 完整 Pipeline 优化

```
> 帮我用 optimize-pipeline 优化 src/math/matrix.c 中的 matmul 函数，
  测试用例是 tests/test_matmul，目标平台 Kunpeng-0xd01
```

Pipeline 会自动执行：项目准备 → 性能基线 → 热点分析 → 策略决策 → 代码优化 → 验证 → 审查。

#### 2. 查询 ARM 指令

```
> vmlaq_f32 和 vfmaq_f32 有什么区别？哪个在 Kunpeng-0xd01 上更快？
```

#### 3. 热点函数分析

```
> 用 perf record 采样 ./my_app，找到 CPU 热点函数和调用链
```

#### 4. 向量化一个循环

```
> 用 NEON intrinsics 向量化下面这个求和循环：
  for (int i = 0; i < n; i++) sum += a[i] * b[i];
```

#### 5. 微架构分析

```
> 用 perf stat 分析 ./my_app 的 IPC、缓存 miss 和分支预测失败
```

---

## 插件机制

KPBot 采用插件化架构，每个插件独立封装，通过 marketplace 统一管理。

### 架构概览

```
marketplace.json          ← 顶层 marketplace 索引（Claude Code）
  ├── code-optimizer      ← 插件（plugin.json + skills/）
  └── app-tuner           ← 插件（plugin.json + skills/）
```

- **Marketplace**（`.claude-plugin/marketplace.json`）：顶层插件索引，声明所有可用插件及其元信息
- **Plugin**（`Plugins/<name>/.claude-plugin/plugin.json`）：单个插件的清单文件，定义名称、版本、描述、关键词等
- **Skills**（`Plugins/<name>/skills/`）：插件的技能目录，每个技能包含一个 `SKILL.md`

### Claude Code Marketplace

```bash
# 安装整个 marketplace
claude plugins marketplace add https://atomgit.com/openEuler/KPBot.git

# 安装单个插件
claude plugins install code-optimizer --marketplace kpbot-marketplace
```

---

## 卸载

### OpenCode

```bash
# 项目级：删除 skills 目录和配置文件
rm -rf .opencode/skills/
rm -f AGENTS.md

# 全局级
rm -rf ~/.config/opencode/skills/
rm -f ~/.config/opencode/AGENTS.md
```

### Claude Code

```bash
claude plugins uninstall code-optimizer
claude plugins marketplace remove kpbot-marketplace
```

---

## 常见问题

### Q: 安装后技能没有被发现？

```bash
opencode debug skill | grep optimize
```

### Q: 全局安装后路径不对？

全局安装时 `install.sh` 会自动将相对路径重写为绝对路径。如果 KPBot 仓库移动了位置，需要重新安装：

```bash
/path/to/KPBot/install.sh global claude
# 或
/path/to/KPBot/install.sh global opencode
```

### Q: 安装时已有配置文件被覆盖？

`install.sh` 在覆盖前会自动备份原文件（`*.bak.<timestamp>`）。全局模式下还支持交互选择：
- **[O] 覆盖** — 用插件内容替换（原内容已备份）
- **[M] 合并** — 插件内容置顶，保留原自定义内容
- **[S] 跳过** — 保持现有文件不变

### Q: 如何更新到最新版本？

重新运行安装脚本即可（会覆盖旧文件并保留备份）：

```bash
cd /path/to/KPBot && git pull
/path/to/KPBot/install.sh
```

---

## 目录结构

```
KPBot/
├── .claude-plugin/
│   └── marketplace.json           # Claude Code Marketplace 索引
├── Plugins/
│   ├── code-optimizer/            # 代码优化插件 (36 skills)
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json        #   插件清单
│   │   ├── skills/                #   技能源目录（claude 格式，单一源）
│   │   └── opencode/              #   OpenCode 覆盖层（26 个差异文件，稀疏镜像 skills/ 结构）
│   └── app-tuner/                 # 应用级调优插件
│       ├── .claude-plugin/
│       │   └── plugin.json
│       └── skills/                #   技能源目录
├── install.sh                     # 统一安装脚本（Claude Code + OpenCode）
├── README.md
└── LICENSE
```

## 添加新插件

1. 在 `Plugins/` 下创建新目录
2. 添加 `.claude-plugin/plugin.json`（插件清单，含 name、version、description、author 等）
3. 在 `skills/` 下创建技能目录（每个技能一个 `SKILL.md`）
4. 在 `.claude-plugin/marketplace.json` 的 `plugins` 数组中添加条目
5. 提交 PR

## License
