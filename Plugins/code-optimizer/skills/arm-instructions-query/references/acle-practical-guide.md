# ACLE AI 辅助向量化实践指南

> 本指南将 Arm C 语言扩展（ACLE）规范提炼为可操作的规则、常见陷阱以及在 Arm 平台上编写正确向量化代码的成熟模式。旨在约束 AI 代码生成，产出正确、可移植、高性能的向量代码。

本指南按章节拆分为多个文件，便于 AI 按需读取对应章节，减少 token 消耗。

---

## 章节列表

| 章节 | 文件 | 说明 |
|------|------|------|
| 快速参考卡 | [`ch01-quick-reference.md`](ch01-quick-reference.md) | 头文件、类型命名、条件编译速查 |
| 类型系统规则 | [`ch02-type-system.md`](ch02-type-system.md) | Neon/SVE/SME 类型差异、可移植性规则 |
| 常见 AI 错误 | [`ch03-common-ai-errors.md`](ch03-common-ai-errors.md) | AI 代码生成中的典型错误与修复方法 |
| 成功模式 | [`ch04-success-patterns.md`](ch04-success-patterns.md) | 经过验证的向量化实现模式 |
| 特性检测宏 | [`ch05-feature-detection-macros.md`](ch05-feature-detection-macros.md) | 运行时/编译期特性检测宏列表 |
| 函数多版本化 | [`ch06-function-multi-versioning.md`](ch06-function-multi-versioning.md) | 按 ISA 分发多版本代码的策略 |
| NEON-SVE Bridge | [`ch07-neon-sve-bridge.md`](ch07-neon-sve-bridge.md) | NEON↔SVE 互操作内联函数 |
| 头文件快速参考 | [`ch08-header-reference.md`](ch08-header-reference.md) | 各头文件提供内容与包含条件 |
| 内存预取内联函数 | [`ch09-prefetch-intrinsics.md`](ch09-prefetch-intrinsics.md) | ARM ACLE 预取 API 使用方法 |
| 对齐注意事项 | [`ch10-alignment.md`](ch10-alignment.md) | 向量类型内存对齐规则 |
| 头文件包含顺序与依赖 | [`ch11-header-dependencies.md`](ch11-header-dependencies.md) | 头文件间的依赖关系与正确包含顺序 |
| SME 流模式深入探讨 | [`ch12-sme-streaming-mode.md`](ch12-sme-streaming-mode.md) | SME 流模式原理与使用场景 |

---

## 使用建议

- **快速查阅**：从 `ch01-quick-reference.md` 开始
- **首次编写向量化代码**：必读 `ch02-type-system.md` + `ch04-success-patterns.md`
- **AI 生成代码审查**：重点参考 `ch03-common-ai-errors.md`
- **性能调优**：参考 `ch09-prefetch-intrinsics.md` + `ch10-alignment.md`
