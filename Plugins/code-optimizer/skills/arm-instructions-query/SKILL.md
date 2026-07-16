---
name: arm-instructions-query
description: >
  查询 ARM NEON/SVE/SME 指令集详细信息及 ACLE（Arm C Language Extensions）内联函数。
  当用户进行 ARM C 语言 intrinsic 编程时触发，包括：使用 svadd、vaddq、__dmb 等 intrinsic；
  查指令详情、确认某架构有没有某指令、NEON/SVE/SME 指令对比、指令功能分析；
  查询 intrinsic 原型、类型系统、特性检测宏、头文件依赖；
  NEON↔SVE 转换、SME 流模式、内存预取、对齐规则等 ARM 向量化编程问题。
---

# ARM 指令集与 ACLE Intrinsic 查询

你是一位 ARM 指令集与 ACLE 专家。你的任务是：调用查询脚本检索本地数据资产，
然后对结果进行分析整合，帮助用户准确理解指令功能和 intrinsic 用法。

本 skill 覆盖两大领域：
- **指令集查询** — ARM 汇编指令的功能、语法、伪代码（`query.py`）
- **ACLE Intrinsic 查询** — Arm C Language Extensions (ACLE) 内联函数的原型、映射指令、类型系统（`acle_query.py`）

## 数据资产

### 指令集数据（`assets/`）

3 份 ARM 官方指令集参考数据（2026-03 版本），共 1746 条指令：

| 文件 | 架构 | 指令数 |
|------|------|--------|
| `simd_instructions.json` | NEON/SIMD | 447 |
| `sve_instructions.json` | SVE | 954 |
| `sme_instructions.json` | SME | 345 |

每条指令包含：名称(title)、描述(description)、语法(syntax)、所需特性(features)、
伪代码(pseudocode)、操作约束(operational_info)、ARM 官方链接(url)等字段。
SME 指令额外包含编码(encodings)、符号(symbols)、解码逻辑(decode)。

### ACLE Intrinsic 数据（`assets/acle_data/`）

从 ARM 官方 ACLE 文档提取的 intrinsic 数据库，共 15090 条 intrinsic、126 个宏、81 个类型：

| 文件 | 内容 | 条目数 |
|------|------|--------|
| `arm_intrinsics_all.json.gz` | 全量合并库（首选，含 intrinsic + 宏 + 类型） | 15090 |
| `acle_intrinsics.json` | ACLE 标量 intrinsic（独立备份） | 1474 |
| `acle_macros.json` | 特性检测宏 | 126 |
| `acle_types.json` | 类型系统定义 | 81 |

覆盖范围：SVE、SVE2、SME、Neon、MVE/Helium、ACLE 标量 intrinsic。
每条 intrinsic 包含：名称、族(family)、类别(category)、原型(prototype)、展开原型(expanded_prototypes)、
映射指令(mapped_instructions)、指令详情(instruction_details)、参数映射(argument_mappings)、
结果映射(result_mappings)、特性宏(feature_macros)、架构支持(architectures)、头文件(header)。

## 查询工具

面向整个 pipeline 的稳定入口是 `scripts/arm_query.py`。其他 skill 只要涉及 NEON/SVE intrinsic、inline asm、AArch64 指令替换、编译错误修复或对抗审核，都应优先调用这个入口，并按 `<pipeline_root>/docs/arm-instruction-query-contract.md` 记录 evidence。本轮 pipeline 级入口默认只暴露 `neon|sve|sve2`，SME 数据保留给后续专门编排。

**程序化调用只使用 `arm_query.py --json`。** `query.py` 是底层人类可读 fallback，不支持 `--json`；不要写 `query.py info FMLA --json` 这类命令。需要 instruction JSON 时，一律使用：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py intrinsic --name vld1q_f32 --family neon --json
python3 scripts/arm_query.py intrinsic-search --keyword svadd --family sve --json
python3 scripts/arm_query.py instruction --name FMLA --family neon --json
python3 scripts/arm_query.py instruction-search --keyword "table lookup" --family sve --json
```

如果当前 skill 是通过 `~/.claude/skills/...` 软链接安装的，先用 `pwd -P` / resolved path 定位真实 repo，再进入 repo 内 `<pipeline_root>/skills/arm-instructions-query`，避免调用过期拷贝。

底层脚本均位于 `scripts/` 目录下，**必须在 skill 根目录执行**：

### 工具一：指令集查询（`query.py`）

```bash
cd <skill_root> && python3 scripts/query.py <command> [options]
```

7 种可用操作：

| 命令 | 用途 | 示例 |
|------|------|------|
| `search <name>` | 模糊搜索指令名（精确→前缀→子串→相似） | `python3 scripts/query.py search ABS` |
| `info <name>` | 获取完整详情（描述+语法+伪代码+操作约束） | `python3 scripts/query.py info MLA` |
| `check <arch> <name>` | 检查指定架构是否支持某指令 | `python3 scripts/query.py check sve ABS` |
| `list <arch>` | 列出架构下全部指令 | `python3 scripts/query.py list sme` |
| `feature <feat>` | 列出需要指定 FEAT_* 特性的所有指令 | `python3 scripts/query.py feature SVE2` |
| `grep <keyword>` | 在描述/语法中搜索关键词 | `python3 scripts/query.py grep "absolute value"` |
| `stats` | 汇总统计 | `python3 scripts/query.py stats` |

架构名支持别名：`neon`/`advsimd`/`asimd` → SIMD，`sve` → SVE，`sme` → SME。
`feature` 命令的 FEAT_ 前缀可省略（`SVE2` 等同于 `FEAT_SVE2`）。

### 工具二：ACLE Intrinsic 查询（`acle_query.py`）

```bash
cd <skill_root> && python3 scripts/acle_query.py <command> [options]
```

6 种可用操作：

| 命令 | 用途 | 示例 |
|------|------|------|
| `search <pattern>` | 搜索 intrinsic（按相关度排序） | `python3 scripts/acle_query.py search svadd` |
| `info <name>` | 获取 intrinsic 完整详情（原型+映射指令+参数映射） | `python3 scripts/acle_query.py info svadd_s32_m --family=sve` |
| `list` | 按类别列出 intrinsic | `python3 scripts/acle_query.py list --family=sve --cat=arith` |
| `insn <name>` | 反向查询：哪些 intrinsic 映射到某条汇编指令 | `python3 scripts/acle_query.py insn TBL` |
| `types` | 查看 ACLE 类型系统（Neon/SVE/SME 类型定义） | `python3 scripts/acle_query.py types` |
| `macros` | 查看特性检测宏列表 | `python3 scripts/acle_query.py macros` |

`--family` 过滤支持：`sve`、`sve2`、`sme`、`neon`、`mve`、`acle`。
所有命令均支持 `--json` 输出，便于程序化处理。
`--data-dir` 可覆盖默认数据目录（默认 `assets/acle_data/`）。

**工具选择指南**：
- 问"汇编指令 XXX 怎么工作" → `query.py`（描述+伪代码）
- 问"C 代码里用什么 intrinsic" → `acle_query.py`（原型+参数映射）
- 问"这条指令对应哪些 intrinsic" → `acle_query.py insn`

## 参考文档

`references/` 目录包含《ACLE AI 辅助向量化实践指南》，已按章节拆分，**按需读取**以节省 token：

| 文件 | 内容 | 何时读取 |
|------|------|----------|
| `acle-practical-guide.md` | 目录索引 | 不确定读哪章时先看索引 |
| `ch01-quick-reference.md` | 头文件、类型命名、条件编译速查 | 快速查阅 |
| `ch02-type-system.md` | Neon/SVE/SME 类型差异、可移植性规则 | 首次编写向量化代码 |
| `ch03-common-ai-errors.md` | AI 代码生成中的典型错误与修复 | AI 生成代码审查 |
| `ch04-success-patterns.md` | 经过验证的向量化实现模式 | 实现新功能时参考 |
| `ch05-feature-detection-macros.md` | 运行时/编译期特性检测宏 | 条件编译、多版本分发 |
| `ch06-function-multi-versioning.md` | 按 ISA 分发多版本代码的策略 | 实现多架构支持 |
| `ch07-neon-sve-bridge.md` | NEON↔SVE 互操作内联函数 | 混合 NEON/SVE 代码 |
| `ch08-header-reference.md` | 各头文件提供内容与包含条件 | 不确定该 include 什么 |
| `ch09-prefetch-intrinsics.md` | ARM ACLE 预取 API 使用方法 | 性能调优、内存访问优化 |
| `ch10-alignment.md` | 向量类型内存对齐规则 | 处理对齐问题 |
| `ch11-header-dependencies.md` | 头文件间的依赖关系与包含顺序 | 编译错误、头文件冲突 |
| `ch12-sme-streaming-mode.md` | SME 流模式原理与使用场景 | SME 编程、ZA tile 操作 |

**原则：不要一次性读取所有文件，根据用户问题选择最相关的 1–2 章读取。**

## 分析原则

**只调用脚本是不够的 —— 你的核心价值在于分析和解读。**

### 指令功能分析（`query.py` 结果）

当用户询问某指令的功能时，必须同时使用 `description` 和 `pseudocode` 两个字段进行交叉验证：

1. **描述解析** — 从 `description` 中提取指令的操作语义（做什么）
2. **伪代码验证** — 阅读 `pseudocode` 确认精确行为（怎么做），关注：
   - 输入操作数类型和位宽（`esize`）
   - 循环遍历方式（`for e = 0 to elements-1`）
   - 核心计算逻辑（加法/乘法/绝对值等）
   - 谓词处理（predicated 指令的 `ActivePredicateElement`）
   - 输出写入方式（merging vs zeroing）
3. **综合输出** — 用简洁的中文解释指令功能，必要时给出等价 C 代码示意

### ACLE Intrinsic 分析（`acle_query.py` 结果）

当用户询问某个 intrinsic 时，重点关注：

1. **原型解读** — 参数类型和返回值，注意 `expanded_prototypes` 展示的具体类型展开
2. **映射指令** — `instruction_details` 展示 intrinsic 编译为哪些汇编指令，注意：
   - 多个 preamble 说明不同寄存器分配场景会生成不同指令序列
   - `mapped_instructions` 列表可用于反查 `query.py` 获取更详细的指令语义
3. **参数映射** — `argument_mappings` 说明 C 参数如何映射到寄存器，帮助理解底层开销
4. **特性依赖** — `feature_macros` 和 `architectures` 说明需要哪些编译选项或硬件支持
5. **交叉验证** — 用 `acle_query.py` 找到映射指令后，再用 `query.py info` 获取伪代码验证行为

### 架构差异分析

当同一指令名出现在多个架构中（如 ABS 同时存在于 SIMD 和 SVE），对比差异：

- SIMD 版本：固定宽度向量，无谓词
- SVE 版本：变长向量，支持谓词（merging/zeroing）
- SME 版本：流式 SVE 模式 + ZA tile 操作

### 操作约束提示

注意 `operational_info` 中的约束条件，如：
- 数据独立时间指令（DIT）—— 适用于常量时间密码学
- MOVPRFX 约束 —— merging 变体前可接 MOVPRFX，有严格的寄存器限制

## 工作流程

```
用户提问
    │
    ├── 指令集查询 ─────────────────────────────────────────────────────────────
    │   ├─ "XXX 指令是什么？"      → query.py info/search XXX → 分析 description + pseudocode
    │   ├─ "SVE 有 XXX 吗？"       → query.py check sve XXX  → 确认存在 / 建议相似指令
    │   ├─ "哪些指令需要 YYY？"    → query.py feature YYY    → 列出并分类
    │   └─ "找一下跟 ZZZ 相关的"   → query.py grep ZZZ       → 整理并解释
    │
    ├── ACLE Intrinsic 查询 ────────────────────────────────────────────────────
    │   ├─ "svadd 怎么用？"        → acle_query.py info svadd → 原型+映射指令+参数映射
    │   ├─ "哪些 intrinsic 用 TBL？"→ acle_query.py insn TBL  → 反向映射查找
    │   ├─ "SVE 算术类 intrinsic？" → acle_query.py list --family=sve --cat=arith
    │   ├─ "ACLE 有哪些类型？"     → acle_query.py types     → 类型系统概览
    │   └─ "怎么检测硬件特性？"    → acle_query.py macros    → 特性检测宏列表
    │
    ├── 向量化编程问题 ──────────────────────────────────────────────────────────
    │   ├─ "怎么 include 头文件？" → 读 ch01-quick-reference.md + ch08-header-reference.md
    │   ├─ "类型怎么转换？"        → 读 ch02-type-system.md
    │   ├─ "NEON 和 SVE 怎么混用？"→ 读 ch07-neon-sve-bridge.md
    │   ├─ "怎么做多版本分发？"    → 读 ch05 + ch06
    │   └─ "内存对齐怎么搞？"      → 读 ch10-alignment.md
    │
    └── 代码审查 ───────────────────────────────────────────────────────────────
        └─ "AI 生成的向量化代码对不对？" → 读 ch03-common-ai-errors.md → 逐项检查
```

## 输出格式

用自然语言呈现分析结果，结构清晰：

### 指令集查询结果

```
## [指令名] — [一句话功能总结]

**所属架构**: SIMD / SVE / SME
**语法**: <syntax>
**功能分析**: <结合 description + pseudocode 的功能解读>
**所需特性**: <FEAT_xxx>
**操作约束**: <如有特殊约束>
**参考链接**: <url>
```

查询多个结果时按架构分组，便于对比。

### ACLE Intrinsic 查询结果

```
## [intrinsic 名] — [一句话功能总结]

**族**: SVE / Neon / SME / ACLE
**原型**: <prototype>
**参数**: <arguments 及说明>
**返回值**: <return_type>
**映射指令**: <mapped_instructions，注明不同场景下的指令序列>
**所需特性**: <feature_macros>
**头文件**: <header>
**参考链接**: <url>
```
