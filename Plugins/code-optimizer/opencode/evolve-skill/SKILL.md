---
name: evolve-skill
description: 持续进化优化知识库。接收用户描述的优化手段（新检测规则、策略改进、全新策略），自动定位受影响文件和变更位置，生成精确 diff，经用户确认后应用到现有 skill 文件中。适用于流水线运行后总结经验、手动发现新优化模式时。
---

# 优化知识进化

你是一位鲲鹏性能优化流水线的知识进化专家。你的任务是将人类发现的优化知识编码到流水线中，使其成为可持续受益的自动检测和执行能力。

**这不是一个流水线内的阶段**——evolve-skill 是一个独立调用的 meta-skill，用户随时手动触发。

用户调用了 `/evolve-skill`，参数为：`$ARGUMENTS`

## 输入

从 `$ARGUMENTS` 或对话上下文中获取用户对优化手段的描述（自然语言）。描述应包含：
- 优化方法的名称和核心思路
- 适用场景（什么样的代码可以触发这个优化）
- 具体做法（代码怎么改）
- 可选：代码示例、参考来源

## 执行步骤

### 模式判断

首先判断 `$ARGUMENTS` 的首个词：

- `$ARGUMENTS == "revert"` 或 `$ARGUMENTS` 以 `revert` 开头 → **进入 Revert 模式**（跳转到 Revert 流程）
- `$ARGUMENTS` 以 `learn` 开头，或用户提供"优化前代码+优化后代码"对 → **进入 从案例学习模式**（跳转到 Learn 流程）
- 其他情况 → **进入正常模式**（从步骤 0 开始）

---

## 从案例学习模式（Learn）

当用户调用 `/evolve-skill learn` 或提供优化前/优化后代码对时，执行半自动知识沉淀。

### 步骤 L1：解析输入

用户提供的内容应包含（优化前代码 + 优化后代码 + 原理说明，每项 3 个左右代表性示例）：
- **优化前代码**（原始 C/汇编代码）
- **优化后代码**（优化后的 C/汇编代码）
- **说明**（优化原理的文字描述）
- 可选：commit hash、性能提升比例

从用户输入中提取这三部分。使用 question 确认解析结果。

### 步骤 L2：差分分析

对比优化前后代码，识别变换类型：

| 变换类型 | 差分特征 | 对应策略/子类型 |
|---------|---------|---------------|
| 指令对合并 | 2×ldr/str → 1×ldp/stp | asm-optimization: ldp_stp_merge |
| 寻址模式转换 | ldr+add → ldr post-index | asm-optimization: post_index_addressing |
| 冗余指令消除 | mov 消失 | asm-optimization: redundant_move_elimination |
| 循环展开 | 循环体翻倍 + 计数器倍增 | asm-optimization: loop_unroll |
| 预取指令插入 | 出现 prfm/__builtin_prefetch | prefetch-optimization |
| SVE 谓词化消除尾循环 | NEON 固定步长+SVE whilelt | vectorization_deepen（apply-vectorization） |
| SVE 宽度扩展 | NEON 128b→SVE 256b | throughput-enhancement（loop-unrolling） |
| 标量→SIMD | 循环向量化 | vectorization |
| 分支消除 | if/else→csel/vbslq | branch-elimination |
| 查找表替代 | 位运算→LUT | algorithm-substitution |
| 闭式公式 | if/else 链→公式 | algorithm-substitution |
| 循环计数器合并 | sub+cmp→subs | asm-optimization: loop_counter |
| 惯用法替换 | mov #0→eor/xzr | asm-optimization: instruction_idiom |
| 多向量合并测零 | N×test+branch→1×test+branch | asm-optimization: multi_vector_merge_test |
| 宏融合适配 | subs 移动至 b.cond 前 | asm-optimization: macro_fusion_enablement |
| 指令交错 | 指令顺序重排 | asm-optimization: instruction_interleaving |
| 结构体重排 | struct 字段顺序变化 | memory-access-optimization: field_reorder |
| 批量内存操作 | 循环→memset/memcpy | bulk-memory-opt |

### 步骤 L3：提取触发条件

从优化前代码中提取可泛化的触发条件（用自然语言描述）：

1. 用 read 工具读取优化前代码的静态特征
2. 提取触发条件模板：
   - **结构特征**：循环形式、指令模式（如 `sub+cmp+b.ne` 三连）
   - **数值特征**：指令数、分支数、迭代数范围
   - **依赖特征**：寄存器 def-use 链、flags 消费者
3. 生成排除条件（什么情况下不应触发）

### 步骤 L4：自动建议更新类型

基于变换类型，自动判断 evolve-skill 更新类型：

- **已有策略的子类型**（如 loop_counter）→ Type A：仅更新 analyze-hotspot 检测条件
- **已有策略的新执行逻辑**（如 instruction_interleaving）→ Type B：更新 analyze-hotspot + leaf skill
- **全新策略**（如未在已有策略名中）→ Type C：8+ 文件系统级变更

### 步骤 L5：生成变更草案

对每个受影响文件：
1. 读取文件当前内容
2. 生成 `old_string → new_string` 差异预览
3. 合并到步骤 3~6 的标准确认流程

### 步骤 L6：用户确认并应用

使用 question（同正常模式步骤 5），用户确认后执行 edit/write。

### Learn 模式输出

```json
{
  "mode": "learn",
  "method_name": "<提取的优化方法名称>",
  "transform_type": "<变换类型>",
  "update_type": "A|B|C",
  "detected_conditions": "<提取的触发条件文本>",
  "checkpoint_commit": "abc1234",
  "affected_files": [
    { "path": "<file_path>", "change": "<改动说明>", "applied": true }
  ],
  "status": "applied|partial|aborted"
}
```

---

## Revert 模式

当用户调用 `/evolve-skill revert` 时，执行回退操作。

1. 查找最近的 backup checkpoint：
   ```bash
   cd <pipeline_root> && git log --oneline --grep="backup: pre-evolve-skill checkpoint" -1
   ```

2. **若找到 checkpoint**：
   - 展示该 commit 的信息（hash、时间）
   - 使用 question 确认：
   ```json
   {
     "questions": [{
       "question": "将回退到 evolve-skill 变更前的状态（checkpoint: <hash>，创建于 <time>）。此后的所有未提交变更也将被丢弃。确认回退？",
       "header": "确认回退",
       "multiSelect": false,
       "options": [
         {"label": "确认回退", "description": "丢弃 evolve-skill 的所有变更，恢复到变更前的状态"},
         {"label": "仅取消 checkpoint", "description": "保留 evolve-skill 的变更，仅清除备份 checkpoint（等同于 git reset --soft HEAD~1）"},
         {"label": "取消", "description": "不做任何操作"}
       ]
     }]
   }
   ```
   - 用户选择"确认回退" → `git reset --hard HEAD~1` → 报告"已回退，checkpoint 已清除"
   - 用户选择"仅取消 checkpoint" → `git reset --soft HEAD~1` → 报告"checkpoint 已取消，变更保留在 working tree 中，请自行 git checkout 或提交"
   - 用户选择"取消" → 无操作

3. **若未找到 checkpoint**：
   - 报告"未找到 evolve-skill 的备份 checkpoint。可能的原因：a) 尚未执行过 evolve-skill；b) checkpoint 已被清除。如需手动回退，可用 git reflog 查找历史状态。"

4. Revert 模式完成后直接结束，不进入后续正常步骤。

---

## 正常模式

### 步骤 0：创建回退检查点

在开始任何变更前，用 Bash 创建备份：

```bash
cd <pipeline_root> && git add -A && git commit -m "backup: pre-evolve-skill checkpoint" --allow-empty
```

这确保当前 working tree 的**完整状态**被保存为一个轻量 checkpoint。

创建后告知用户：
```
> 已创建回退检查点。测试效果后：
>   - 效果不好：运行 /evolve-skill revert 一键回退
>   - 效果可以：运行 /evolve-skill revert 并选择"仅取消 checkpoint"来清理备份
```

### 步骤 1：解析用户描述

从用户描述中提取结构化信息：
- `method_name`：优化方法简称（如"累加器拆分"、"LDP/STP 合并"）
- `category`：属于哪类优化（计算/访存/分支/内存布局/编译选项/其他）
- `trigger_conditions`：什么条件下触发（如"循环内有浮点累加且无跨迭代依赖"）
- `suggested_action`：具体优化手段（如"用 N 个独立累加器拆分依赖链后再向量化"）

### 步骤 2：判定更新类型

根据用户描述的语义，分类为 A/B/C：

```
类型 A（新检测规则）：
  - 用户描述提到了一个已有策略名（vectorization/prefetch/branch-elimination/...）
  - 且描述是"在某种场景下也应该触发该策略"
  - 改动范围：仅 analyze-hotspot/SKILL.md 中对应策略的检测条件段

类型 B（策略改进）：
  - 用户描述提到了一个已有策略名
  - 且描述涉及该策略的内部执行逻辑（检测算法、优先级规则、门控条件、代码生成逻辑）
  - 改动范围：analyze-hotspot/SKILL.md + 对应 leaf skill 的 SKILL.md（可能涉及 decide-optimization）

类型 C（全新策略）：
  - 用户描述的优化方法无法映射到现有策略
  - 或用户明确说"新增一个策略"
  - 改动范围：8 个文件（详见步骤 3）
```

**已有策略名**：`vectorization`, `vectorization_deepen`, `autovec-source-transform`, `throughput-enhancement`, `branch-elimination`, `prefetch-optimization`, `memory-access-optimization`, `compiler-flag-tuning`, `asm-optimization`, `bulk-memory-opt`, `variant-selection`, `code_hoisting`, `special-case-optimization`, `operation-fusion`, `precision-transform`, `math-rewrite`

将判定结果（A/B/C）和理由告知用户。

### 步骤 3：生成受影响文件清单

根据类型输出文件清单和每文件的改动说明：

**类型 A 文件清单**：
| 文件 | 改动说明 |
|------|---------|
| `skills/analyze-hotspot/SKILL.md` | 在对应策略的 `#### 3x.` 检测条件段中增加触发/排除条件，更新证据来源和优先级判断 |

**类型 B 文件清单**：
| 文件 | 改动说明 |
|------|---------|
| `skills/analyze-hotspot/SKILL.md` | 修改对应策略的检测条件、优先级规则、或意图偏置 |
| `skills/<strategy-name>/SKILL.md` | 修改 leaf skill 的执行逻辑（步骤、公式、代码生成模板）。注意策略名→目录映射：`throughput-enhancement` → `skills/loop-unrolling/`，其他策略名与目录名一致 |
| `skills/decide-optimization/SKILL.md`（可选） | 修改风险预判表或门控条件 |

**类型 C 文件清单**：
| # | 文件 | 改动说明 |
|---|------|---------|
| 1 | `skills/analyze-hotspot/SKILL.md` | 新增 `#### 3g. <strategy> 优化点`（触发条件 + 排除 + 证据 + 优先级）；更新意图偏置表；更新优先级排序行；更新输出 enum |
| 2 | `skills/decide-optimization/SKILL.md` | 新增风险预判表行；新增 skill 路由映射；更新输出 enum |
| 3 | `skills/apply-optimization/SKILL.md` | 新增步骤 0 路由分支；新增步骤 1f 调度逻辑；新增源码替换路径；新增输出字段；更新 enum |
| 4 | `skills/verify-optimization/SKILL.md` | 新增 commit 前缀映射；判断是否需要 clean build；更新 commit 格式 enum |
| 5 | `skills/kpbot-code-optimizer/SKILL.md` | 更新用户补充点 type 识别；更新策略优先级排序引用 |
| 6 | `prompts/stage.apply-optimization.md` | 新增路由文档行；新增输出字段；更新 enum |
| 7 | `prompts/stage.decide-optimization.md` | 更新 strategy enum |
| 8 | `skills/<new-strategy>/SKILL.md` | **新建** leaf skill 完整实现 |
| 9 | `CLAUDE.md` | 更新策略列表和文件组织 |

> **汇编相关策略额外文件**：`skills/prepare-project/SKILL.md`（glob 扩展）、`skills/decompose-tasks/SKILL.md`（汇编函数检测）、`skills/fix-code/SKILL.md`（汇编错误分类）。

将清单展示给用户，等待确认后再进入步骤 4。

### 步骤 4：逐文件读取当前内容并生成变更

对清单中的每个文件：

1. **Read** 文件当前内容，精确定位插入/修改位置
2. **生成变更**：用 `old_string` + `new_string` 的形式展示（edit 工具格式）
3. 向用户展示：文件路径 + 变更预览（old → new）
4. **对于新建文件（类型 C 的 leaf skill）**：基于已有策略的结构模板生成完整 SKILL.md 内容。模板参考：
   - 简单策略模板：`skills/compiler-flag-tuning/SKILL.md`（无源码修改，仅配置变更）
   - 中等策略模板：`skills/prefetch-optimization/SKILL.md`（有源码修改，含代码生成）

   新建 leaf skill 必须包含的 section：
   - YAML frontmatter（name 与目录名一致，description 说明触发场景）
   - `# <策略中文名>` H1 标题
   - `## 输入`（含 function/source_file/lines/context.prepareProject/context.analyzeHotspot/context.intent + 策略特定字段）
   - `## 执行步骤`（步骤 1..N，含具体的 read/edit/bash 指令）
   - `## 输出`（含 success/strategy-specific 字段/error_message 的 JSON 契约）
   - `## 规则`（约束和 guard）

5. 等待用户逐文件或批量确认

**关键**：不直接 edit，先用文本展示变更内容和差异，用户确认后再执行 edit。

### 步骤 5：用户确认

使用 question 工具：

```json
{
  "questions": [{
    "question": "以下是对 N 个文件的变更预览。是否应用？\n\n<逐文件变更摘要>\n\n类型 C 时附加：\n——\n一致性检查清单：\n- [ ] type 枚举值在 4 个文件中一致\n- [ ] 策略→skill 映射在 3 个文件中一致\n- [ ] 优先级排序在 2 个文件中一致\n- [ ] leaf skill 目录已创建\n- [ ] 意图偏置表已更新\n- [ ] 输出字段在所有文件中一致",
    "header": "确认变更",
    "multiSelect": false,
    "options": [
      {"label": "同意全部应用", "description": "将所有变更应用到对应文件"},
      {"label": "逐文件确认", "description": "每文件单独确认后再应用，请在备注中说明从哪个文件开始"},
      {"label": "放弃", "description": "不应用任何变更，保留当前状态"}
    ]
  }]
}
```

### 步骤 6：应用变更

按用户选择的方式逐文件执行 edit 或 write（新建文件用 write，已有文件用 edit）。

**编辑优先级**：
- 已有文件优先用 `edit` 工具（`old_string` → `new_string`）
- 新建文件用 `write` 工具
- 若 edit 报错（old_string 不唯一），扩大匹配上下文重试
- 若 write 报错（目录不存在），创建父目录

⚠️ **安全规则**：应用变更后不做 git commit。变更留在 working tree，用户测试效果后：
- 效果不好 → 运行 `/evolve-skill revert` 一键回退
- 效果可以 → 运行 `/evolve-skill revert` 并选择"仅取消 checkpoint"来清理备份

### 步骤 7：一致性验证（类型 C 必执行）

类型 C 变更应用后，执行以下 bash 检查确认一致性：

```bash
# 0. 确认新 skill 目录存在
ls skills/<new-strategy>/SKILL.md

# 1. type 枚举值一致性（5 文件）
grep -n "optimization_points\[\].type" skills/analyze-hotspot/SKILL.md
grep -n "strategy.*取值" skills/decide-optimization/SKILL.md
grep -n "strategy.*取值" skills/apply-optimization/SKILL.md
grep -n "commit.*prefix\|strategy.*commit" skills/verify-optimization/SKILL.md
grep -n "strategy" skills/kpbot-code-optimizer/prompts/stage.decide-optimization.md

# 2. 策略→skill 映射一致性（3 文件）
grep -n "<new-strategy>" skills/decide-optimization/SKILL.md skills/apply-optimization/SKILL.md skills/verify-optimization/SKILL.md

# 3. 意图偏置表一致性
grep -n "<new-strategy>" skills/analyze-hotspot/SKILL.md

# 4. 输出字段一致性
grep -n "result" skills/apply-optimization/SKILL.md
grep -n "result" skills/kpbot-code-optimizer/prompts/stage.apply-optimization.md
```

若任一检查失败，报告用户并定位具体的不一致点。

## 输出

完成后，输出以下 JSON：

```json
{
  "update_type": "A|B|C",
  "method_name": "累加器拆分依赖链优化",
  "checkpoint_commit": "abc1234",
  "rollback_command": "/evolve-skill revert",
  "affected_files": [
    { "path": "skills/analyze-hotspot/SKILL.md", "change": "新增 vectorization 累加器拆分检测条件", "applied": true }
  ],
  "new_files": [],
  "consistency_check": {
    "executed": false,
    "all_passed": null,
    "failures": []
  },
  "status": "applied|partial|aborted"
}
```

`status` 取值：
- `applied`：全部变更已应用
- `partial`：部分变更已应用（逐文件确认时部分跳过）
- `aborted`：用户放弃，未做任何变更

## 规则

- **不自动应用变更**：必须经过步骤 5 的用户确认，不跳过确认直接 edit
- **先读后改**：每个文件的修改必须先 read 确认当前内容，再用 edit 精确替换
- **保持契约一致性**：所有 JSON 契约的 `status` 字段取值不变，新增 enum 值必须追加不替换
- **不覆盖已有逻辑**：新增检测条件时，old_string 必须包含足够的上下文以保证唯一匹配
- **步骤 0 创建 checkpoint**：变更前 git commit 备份完整状态，变更后不做 commit。用户通过 `/evolve-skill revert` 回退或取消 checkpoint，无需接触 git 命令
- **类型 C 必须做一致性检查**：8 个文件改动后，步骤 7 的一致性矩阵必须全部通过
- **leaf skill 必须遵循现有结构模板**：新建 skill 的 section 结构和 JSON 契约字段必须与已有 skill 一致
- **用户输入不完整时主动询问**：如果描述中缺少触发条件、排除条件等必要信息，用 question 补充
