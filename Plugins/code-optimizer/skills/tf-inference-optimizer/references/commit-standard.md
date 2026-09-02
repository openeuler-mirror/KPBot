# 提交规范（含构建修复 rebase 规则）

对齐优化目标 TF fork 源码仓库内 `tensorflow/core/grappler/optimizers/ONLINE_OPTIMIZATION_COMMIT_STANDARD.md` 的核心要求，并补充外部构建协作的 rebase 规则。

## 1. 单 commit 完整性

实现 + 聚焦测试 + 每特性文档 + summary 文档更新应放**同一 commit**。禁止把实现、测试、文档拆成多个 commit。

## 2. 性能声明

每个性能声明必须：
- 写明受益模型（哪个模型受益）。
- 写明负面/未测模型（哪些模型有负面影响或未验证）。
- 禁止只报受益不报负面。

## 3. 提交边界

- 实验性实现、被拒绝的实现**不进**生产 commit 序列。
- 单优化点单独 commit，禁止多个优化点混在一个 commit 导致无法归因。

## 4. 构建修复 rebase 规则（外部构建协作）

优化点 commit 后交由开发者/CI 构建。构建过程中发现的问题按以下规则处理：

```
用户回传构建错误 → 定位并修复
  → 修复内容必须 rebase 进之前的特性 commit（特性 commit 保持单一、完整）
  → 禁止遗留独立的 fix commit 在特性 commit 之上
```

原因：保持「一个优化点 = 一个完整 commit」的可归因性，避免 fix commit 散落污染 git 历史与收益归因。

## 5. 正确性约束

- 正确性用例必须全部通过（口径由用户定义，浮点重排不可接受）。
- 复用与独立 op 相同的 functor 保证 bit 级等价。
