---
name: faiss-source-analysis
description: 从当前 Faiss 检出追踪索引训练、add、search、数据布局和内核分派，给出可定位的源码证据。适用于调用链、版本能力、模板选择和实际后端调查。
---

# Faiss 源码分析

## 输入

源码路径、检出版本/改动、目标索引或符号，以及可用的调用方或测试程序。

## 执行步骤

1. 记录 commit、工作区差异、目标类型和已启用构建能力；区分上游接口与可选扩展。
2. 从真实调用方追踪 train/add/search，展开包装索引、模板、metric/decoder 分派与尾段。
3. 说明数据布局、所有权、索引/查询状态和跨层参数传递；对源码行号绑定检出身份。
4. 将源码事实、推断和待验证项分开；运行时内核选择交由构建、反汇编和采样核验。

## 输出

`SOURCE-EVIDENCE.md`：源码身份、文件/符号/行号、调用链、关键布局和未确认项。

## 约束

不按其他版本的行号或扩展目录推定当前能力；找不到的符号标记为未发现，不补造实现。

## 参考资料

- [证据协议](references/source-evidence-protocol.md)
- [源码导航](references/faiss-source-map.md)
- [RaBitQ 调查示例](references/rabitq-source-example.md)
