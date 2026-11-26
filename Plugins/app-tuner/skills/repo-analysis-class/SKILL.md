---
name: repo-analysis-class
description: >
  
  分析代码仓库并生成类型关系视图（类图/接口图）。当用户提到以下场景时触发：类图、继承关系、接口实现、类之间怎么依赖、类型关系、组合与聚合、虚函数体系、trait/接口体系。产出带源码证据锚定的
  Markdown 报告：Mermaid 类图 + 每类型每关系的证据表 + 裁剪声明。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-class：类型关系视图（类图）

# 什么时候用本 skill

用户想看代码里的**类型契约体系**：谁继承谁、谁实现哪个接口、谁持有谁。适合面向对象味浓的仓（C++ 类体系、Java、Python class 层、Rust trait）。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## 类型怎么选（不是全量导入）

- 只画**承载核心契约**的类型：接口/抽象基类 + 其主要实现 + 核心数据持有者，8~12 个
- 判定优先级：被多处依赖的接口 > 继承树根 > 关键聚合根；数据搬运 DTO/工具类默认不进图
- 成员只选**契约面**：公开方法（表达能力的）、关键状态字段（表达职责的）；getter/setter/私有细节不列

## 五种关系的判定证据（每条关系给一条 sources）

| 关系 | 判定证据 |
|---|---|
| 继承 inheritance | `class A : public B` / `class A(B)` / `extends` |
| 实现 realization | `implements` / 纯虚函数的 override / `impl Trait for Type` |
| 组合 composition（拥有生命周期） | 值成员 / `unique_ptr` 成员 / 构造中 `new` 并持有 / `__init__` 中创建的成员 |
| 聚合 aggregation（引用不拥有） | 裸指针或引用成员 / 外部注入的依赖 / `shared_ptr` / 参数传入后保存 |
| 依赖 dependency（临时使用） | 方法参数 / 局部变量 / 一次性调用 |

区分要点：**组合 vs 聚合看生命周期归属**——成员随宿主构造/析构的是组合；成员在宿主之外仍然存活的是聚合。拿不准时降级为依赖并在注记说明。

## 接口语义

- 抽象基类/纯接口画成接口，标注"无实现成员"
- 同一接口多个实现：每个实现一条实现边，不合并
- 模板/泛型类：画具体化后的主要实例，注记"模板，图中按 `<主要实例化>` 展示"

# 领域诚实约束

- 关系方向必须从代码证据读出，**不从"逻辑上应该有关"推断**
- 循环依赖如实画，不为了"图好看"删边；注记说明循环的存在与位置
- 继承层次超过 4 层时展示主链，其余折叠进裁剪说明，声明折叠范围

# IR 骨架

```json
{
  "diagram": "class",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "elements": [
    {
      "id": "model_executor", "label": "ModelExecutor", "kind": "interface",
      "attributes": [{ "name": "runners", "type": "dict[str, ModelRunner]", "visibility": "private" }],
      "methods": [{ "name": "run_prefill", "abstract": true }],
      "sources": [{ "path": "src/executor.py", "line": 31, "end_line": 32, "label": "ABC 定义" }]
    },
    { "id": "r9", "role": "relationship", "from": "pypto_executor", "to": "model_executor", "kind": "realization", "sources": [{ "path": "src/pypto_executor.py", "line": 49, "end_line": 49, "label": "class PyptoExecutor(ModelExecutor)" }] }
  ],
  "notes": ["…"]
}
```

# Mermaid 映射

```
classDiagram
  class ModelExecutor {
    <<interface>>
    +run_prefill()*
    +run_decode()*
  }
  class PyptoExecutor {
    -_runners: dict
  }
  class QwenRunner {
    +run_prefill()
  }
  ModelExecutor <|.. PyptoExecutor
  PyptoExecutor *-- QwenRunner : _runners
  AsyncEngine *-- EngineCore : cores
```

- 继承 `<|--`；实现 `<|..`；组合 `*--`；聚合 `o--`；依赖 `..>`
- 接口块用 `<<interface>>` 标注；抽象成员带 `*` 后缀
- 成员只列契约面（实测教训：全量成员让 Mermaid 类框撑爆画布）

# 输出

按执行链交付：IR 落盘 → repo-view-verify 自验 → `<repo>-class-view.md`（报告契约见 repo-view-render）。
