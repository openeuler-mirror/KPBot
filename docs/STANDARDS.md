# KPBot Skills 开发规范

> 命名规范、结构规范、分类体系、代码规范。
>
> 📖 架构设计 · 贡献指南 · 治理规范 · README

---

## 规范速查

| 规范类别 | 核心规则 | 详细章节 |
| --- | --- | --- |
| 命名 | `{domain}-{name}`，kebab-case | [通用命名规范](#通用命名规范) |
| 结构 | `SKILL.md` + `references/` 渐进式披露 | [Skills 开发规范](#skills-开发规范) |
| 分类 | 知识库 / 工程模板 / 调试与测试 / 测试开发 / 工具辅助 | [Skills 分类体系](#skills-分类体系) |
| 信息来源 | 必须来自可信源，禁止编造 | [信息来源规范](#信息来源规范) |

---

## 通用命名规范

- Skills、Agents 统一采用 domain 前缀命名方式：`{domain}-{name}`
- 命名建议：
  1. 使用小写字母和连字符（kebab-case）
  2. 前缀应简短且具有描述性
  3. `name` 应清晰表达用途/职责
  4. 避免使用缩写，除非是广泛认可的（如 CPU、UT、ST）

---

## Skills 开发规范

### Skills 分类体系

#### 分类原则

Skills 按功能定位分为五大类，每类技能有明确的职责边界和使用场景。

---

#### 一、知识库类

**定位**：提供领域知识和参考资料，作为其他技能的知识源。

**主要职责**：

- 提供特定领域的概念解释、规则说明、参考资料、设计原则。
- 为其他 Skill 提供上下文知识、约束条件、最佳实践和判断依据。
- 支持 Agent 在规划、推理、生成方案、校验结果时进行知识查询。

**不适用范围**：

- 不主动发起任务。
- 不直接修改外部系统数据。
- 不调用具有副作用的外部接口。
- 不执行下单、审批、发布、删除、写入等业务动作。
- 不替代任务型 Skill 完成复杂工作流。
- 不作为最终决策的唯一依据，除非业务明确授权。

**设计原则**：

1. **被动调用模式**：知识库类 Skill 必须遵循被动调用模式。
   - 只有在被 Agent、用户或其他 Skill 显式调用时才返回内容。
   - 不主动启动任务。
   - 不主动调用其他业务系统。
   - 不主动向用户推送建议，除非上层 Agent 明确请求。

2. **无副作用原则**：知识库类 Skill 的所有行为都应是**只读型**。
   - 允许：
     - 查询文档。
     - 检索知识片段。
     - 返回参考资料。
     - 汇总规则。
     - 解释概念。
     - 给出最佳实践建议。
   - 禁止：
     - 写数据库。
     - 调用生产系统变更接口。
     - 提交表单。
     - 创建、删除、修改业务对象。
     - 执行审批、发布、部署等操作。

3. **来源可追溯原则**：知识库类 Skill 输出内容时，应尽可能提供来源依据。
   - 来源可以包括：
     - 内部文档编号。
     - 规范名称。
     - 版本号。
     - 知识条目 ID。
     - 文档路径。
     - 更新时间。
     - 适用范围。
     - 置信度或可靠性等级。

4. **领域边界清晰原则**：知识库类 Skill 应聚焦于一个相对清晰的知识领域。
   - 例如：
     - `design-knowledge-skill`
     - `java-backend-best-practices-skill`
     - `cloud-native-architecture-knowledge-skill`
     - `enterprise-security-policy-skill`
     - `data-governance-reference-skill`

5. **frontmatter 规范**：frontmatter 应保持简洁，不要放复杂治理信息。

   **必填字段**：

   - `name`：小写字母、数字、短横线；通常与目录名一致。
   - `description`：说明 Skill 做什么，以及什么时候使用。

   **举例**：

   ```yaml
   ---
   name: api-design-knowledge
   description: Provides passive, read-only API design knowledge, including guidelines, best practices, examples, terminology, and reference material. Use when an agent or skill needs knowledge lookup for REST API design, API naming, versioning, error codes, OpenAPI documentation, or compatibility rules.
   ---
   ```

   **推荐可选字段**：

   ```yaml
   ---
   name: api-design-knowledge
   description: Provides passive, read-only API design knowledge. Use when an agent needs reference material for API design, error codes, versioning, or OpenAPI documentation.
   license: Proprietary
   compatibility: Agent Skills-compatible runtime. Read-only skill with no side-effect operations.
   metadata:
     skill_type: knowledge_base
     category: guideline
     domain: api_design
     activation_mode: passive
     side_effect_free: "true"
     version: "1.0.0"
     owner: platform-architecture-team
   ---
   ```

6. **正文规范**：
   - **知识内容组织规范**：知识内容应放在 `references/` 目录中，并尽量结构化。
   - **输出规范**：知识库类 Skill 的输出应尽量结构化，避免只输出大段自然语言。

   **举例**：

   ```json
   {
     "answer": "API 错误码应保持稳定、可读、可分类，并避免暴露内部实现细节。",
     "key_points": [
       "错误码应具有唯一性和稳定性。",
       "错误信息应面向调用方可理解。",
       "不应暴露数据库异常、堆栈信息等内部细节。"
     ],
     "recommendations": [
       "建议采用业务域 + 错误类型 + 具体错误的命名方式。"
     ],
     "references": [
       {
         "title": "API Design Guide",
         "section": "5.3 Error Code Design",
         "version": "v1.4"
       }
     ],
     "confidence": "high",
     "limitations": [
       "具体错误码格式应以组织统一规范为准。"
     ]
   }
   ```

7. **安全规范**：即使知识库类 Skill 是只读的，也需要安全边界。
   - 必须遵守：
     - 不输出密钥、Token、证书。
     - 不输出未脱敏的个人信息。
     - 不泄露超出调用方权限的内部资料。
     - 不调用有副作用的外部接口。
     - 不把经验建议伪装成正式制度。
     - 无依据时给出高置信度结论。

**代表性技能**：

| 技能名称 | 用途 |
| --- | --- |
| `kunpeng-kb-arm-instructions-query` | ARM 指令查询 |
| `kunpeng-kb-microarch` | Kunpeng 微架构 |

---

#### 二、工程模板类

**定位**：提供工程脚手架和项目模板，加速项目初始化。

**特点**：

- 提供标准化的需求开发工程结构
- 包含可复用的代码模板
- 降低项目搭建门槛
- 降低上传 openeuler 社区门槛

**代表性技能**：

| 技能名称 | 用途 |
| --- | --- |
| `ub-dev-workflow-template` | UB 新增需求全流程开发模板 |
| `ub-test-template` | UB 新增需求验证用例模板 |
| `kp-openeuler-auto-push-template` | UB/Kunpeng patch 自动化上传 openeuler 社区并跟踪合入模板 |

---

#### 三、调试与诊断类

**定位**：用于问题诊断和调试，帮助定位和解决问题。

**特点**：

- 主动执行调试流程
- 提供系统化的问题排查方法
- 包含诊断工具和脚本
- 包括领域知识及典型案例

**代表性技能**：

| 技能名称（示例） | 用途 |
| --- | --- |
| `kdebug-code-crash` | 代码问题定位子模块，处理崩溃代码层面的问题 |
| `kdebug-code-oom` | 代码问题定位子模块，处理 oom 代码层面的问题 |
| `kdebug-ub-cdma` | 处理 UB 子模块-CDMA（Crystal DMA）问题，涵盖 CQE 超时/状态码、通路寄存器定界、KSVA/TLB/ummu、Reset 流程、事件子系统、驱动加载、Segment 注册等。由 KDebug 在领域识别后按需加载 |

---

#### 四、测试开发类

**定位**：用于测试设计、测试开发和问题定界。

**特点**：

- 主动执行调试流程
- 提供测试迭代全流程调用链
- 提供测试策略、方案、用例设计方法
- 提供测试脚本开发方法
- 提供测试执行时问题定界

**代表性技能**：

| 技能名称 | 用途 |
| --- | --- |
| `ub-test-design-generator` | UB 领域通用测试设计生成工具，生成 Xmind 测试方案 |
| `ub-test-case-generator` | 测试用例设计 |
| `ub-test-script-generator` | UB 测试脚本开发，完成用例脚本生成-评估-优化 |
| `ub-version-issue-reporter` | 问题定界并输出定界报告（报告中含测试用例步骤、测试日志） |

---

#### 五、工具辅助类

**定位**：提供辅助工具和实用功能，提升开发效率。

**特点**：

- 提供具体的功能工具
- 可独立使用或配合其他技能
- 包含脚本和自动化工具

**代表性技能**：

| 技能名称 | 用途 |
| --- | --- |
| `kptools-env-check` | 环境检查（CPU 设备、BIOS/OS 配置、UB 配置） |
| `kptools-frequency-collector` | 采集 core/uncore 主频 |

> 查看完整技能列表：`ls skills/`

---

### 分类使用指南

**选择技能时**：

1. 需要参考资料 → 查询知识库类
2. 需要搭建工程 → 使用工程模板类
3. 遇到问题 → 使用调试与诊断类
4. 需要测试设计、执行、定位或生成测试脚本时 → 使用测试开发类
5. 需要工具支持 → 使用工具辅助类

**组合使用**：

- 开发流程通常需要组合多个技能。
- 知识库类作为基础，模板类加速启动，调试类解决问题，测试开发类保障质量，工具辅助类提升效率。

---

### 创建流程

使用 `skill-creator` 技能创建和优化技能：

直接调用 `skill-creator` 技能即可，它会自动处理：

- 需求分析和规划
- `SKILL.md` 编写
- 测试和评测
- 迭代优化
- 打包发布

详细流程请参考 `skill-creator` 技能。

---

### SKILL.md 结构

```markdown
---
name: skill-name
description: 技能描述（包含触发条件）
---

# Skill Name

## 工作流程
1. 步骤一
2. 步骤二

## 脚本工具
- `scripts/main.py` - 主脚本

## 参考资料
- `references/guide.md` - 详细指南
```

---

### 设计原则

1. **单一职责** —— 每个技能专注一个明确的任务。
2. **可组合性** —— 技能之间可以组合使用。
3. **知识依赖单向性** —— 标识真源 skill（即某领域知识的唯一上游数据源），下游 skill 消费上游数据，禁止反向自引用。修改真源数据时只在真源处更新，避免下游各处漂移。

> 通用原则遵循 `AGENTS.md` 核心原则：信息来源可信、渐进式披露、简洁精炼。

---

### 信息来源规范

- 信息必须来自可信源，禁止编造。
- 知识库类 Skill 输出内容时，应提供来源依据（文档编号、规范名称、版本号、文档路径、更新时间、适用范围、置信度等）。

---

## Agents 开发规范

### 创建 Agent

```markdown
# agents/{agent-name}/AGENT.md
---
name: agent-name
description: Agent 的简短描述
mode: subagent
skills:
  - skill-1
  - skill-2
---

## 职责范围

### 负责
- 任务1

### 不负责
- 任务2
```

---

### Agent 职责分类

| 分类 | Agent 名称 | 核心职责 | 典型场景 |
| --- | --- | --- | --- |
| 代码优化 | `kpbot-code-optimizer` | 代码优化（循环展开，向量化，预取，内存布局，加速指令等） | 底层性能库优化时，如 CRC、GEOHash 等算子库优化时 |
| 优化类 | `kpbot-app-tuner` | 应用调优（BIOS/OS/应用配置/编译器等优化） | 需要对服务器应用进行优化，如需要对 A+K/MySQL 应用场景进行性能优化 |
| 检视类 | `kpbot-code-summarizer` | 代码结构摘要、侧别识别 | 检视流程 Stage 0 |
| 问题定界 | `kpbot-fault-localization` | 鲲鹏测试用例失败问题定界 | 测试问题/环境问题/开发问题 |
| 设计类 | `kpbot-ub-designer` | UB 需求分析和详细设计，生成可直接用于开发的 spec | UB 驱动代码开发前需求分析和设计 |
| 开发类 | `kpbot-ub-developer` | 根据 UB 设计者的 spec 开发对应的需求代码 | UB 新需求代码开发 |
| 验证类 | `kpbot-ub-verifier` | 开发完成的 UB 代码编译、UT 测试、STC 验证 | 验证新开发的 UB 新需求代码，确保代码可信可用 |

---

### 参考示例

查看 `plugins-official/` 下各 Team 目录中的 Agent 实现：

- `kpbot-code-optimizer/agents/` —— 底层性能库优化子 Agent（architect / developer / reviewer）
- `kpbot-app-tuner/agents/` —— 应用优化子 Agent（planner / workflow / optimizer）
- `kpbot-fault-localization/agents/` —— 测试用例失败定界子 Agent
- `kpbot-ub-designer/agents/` —— UB 需求设计子 Agent
- `kpbot-ub-developer/agents/` —— UB 需求代码开发子 Agent
- `kpbot-ub-verifier/agents/` —— UB 代码验证子 Agent

---

## Teams 配置

### 示例：ops-direct-invoke

**核心理念**：Spec-driven Development（规格驱动开发）。

**四阶段工作流**：

1. **设计阶段** —— 需求分析 → 方案设计 → 测试设计
2. **开发阶段** —— 迭代式开发（骨架 → 整合 → 全量），算子代码 + ST 用例 + UT
3. **验收阶段** —— 精度验收 → 性能验收
4. **上库阶段** —— 代码检视 → 开发总结

详细配置见 `plugins-official/ops-direct-invoke/AGENTS.md`。

---

## 代码规范

- 遵循 **PEP 8**。

- 命名约定：

  | 类型 | 约定 | 示例 |
  | --- | --- | --- |
  | 文件名 | 小写下划线 | `skill_loader.py` |
  | 类名 | 大驼峰 | `SkillLoader` |
  | 函数/方法 | 小写下划线 | `load_skill()` |
  | 常量 | 大写下划线 | `MAX_RETRIES` |

---

## 目录结构规范 —— 公共检视修改

各层级资源目录的定位和用途：

| 层级 | 目录 | 用途 | 内容示例 |
| --- | --- | --- | --- |
| 领域根目录 | `skills/` | KPBot Skills 鲲鹏领域软件开发/优化/问题定位等 skills | — |
| Skill 内 | `references/` | 按需加载的知识文档 | API 指南、最佳实践、约束说明 |
| Skill 内 | `assets/` | 输出时使用的静态资源 | 模板文件、图标 |
| Skill 内 | `scripts/` | 可执行脚本 | 数据生成、验证脚本 |
| Team 内 | `workflows/` | teams 流程配置文件 | 任务提示词、数据流定义、错误处理指南 |
