---
name: kitex-thrift-codec-selection
description: 当需要判断 Kitex 是否该启用 frugal、或编解码在火焰图中占比偏高时使用。交付编解码实现的选型依据、回归清单与回退方式。
---

# Kitex Thrift 编解码选型

本 skill 只做"用哪种编解码"的决策与验证，不做协议格式变更，也不做连接形态与 GC 调优。

## 何时触发

- 想确认默认编解码是否已经是较优选择。
- 考虑引入 frugal，或已在用但收益不明显。
- 火焰图中编解码帧占比偏高，需要判断是"实现问题"还是"payload 形态问题"。

## 范围

- 覆盖：fastcodec / frugal / Apache Thrift 的选型、启用条件与回归。
- 不覆盖：线路协议、连接形态、分配热点定位、GC 参数。

## 出处

- 默认实现与切换入口：`pkg/remote/codec/thrift/`（版本以现场读取为准）。
- frugal 的启用条件与标签要求：frugal 仓库 README。

## 命中验证与越界验证

- 命中：问"要不要为了性能换 frugal"，应答"先看编解码占比与 IDL 形态，再用本轮 A/B 判断"。
- 越界：问"该用 Framed 还是 TTHeader"，应答属于协议选型，转 `kitex-transport-protocol-selection`。

## 三种实现

| 实现 | 生成/运行方式 | 适用 | 注意 |
|---|---|---|---|
| fastcodec（默认） | 代码生成，单次分配 + 零拷贝读 | 绝大多数场景 | 默认已启用，通常不需要替换 |
| frugal | 无生成代码，靠结构体标签；纯 Go | 希望减少生成代码、跨服务统一 | 对吞吐不要预设收益；需 IDL 标签与生成选项配合 |
| Apache Thrift | 通用库实现 | 兼容性兜底 | 明显更慢，只作对照或兜底 |

## 执行步骤

1. **确认当前实现**：检查两侧是否使用默认编解码，是否已有自定义 codec，两侧是否一致。
2. **量化编解码占比**：用 CPU profile 看编解码帧占比；占比很低时不要为此做改造。
3. **看 payload 形态**：大字符串/二进制、长整型列表、字段数量多的结构，编解码成本差异更大；先用 IDL 设计削减不必要字段。
4. **做单变量对比**：同一负载下切换实现，3 次取中位，同时看 QPS、P99、每请求分配。
5. **回归**：切换后必须验证错误返回、可选字段、嵌套结构、列表/映射等边界；两侧版本不一致时先对齐。

## 判断要点

- 默认编解码已经是"单次分配 + 零拷贝读"，在小包场景下编解码通常不是主成本；先确认它在 profile 中的占比再决定是否投入。
- frugal 的主要价值是**减少生成代码与维护成本**；对吞吐不要预设收益。公开资料中"快数倍"的对比对象通常是 Apache Thrift，而不是默认实现。
- 若目标是降低分配而不是提速，优先看 `kitex-allocation-hotspot-tuning` 与 IDL 形态，而不是换实现。
- 只有当基准中目标 IDL 明确为正收益、且回归通过时，才建议切换。

## 回退

- 恢复默认编解码，去掉自定义 codec 选项。
- 保留旧的生成代码与 IDL 标签版本，避免回退时重新生成不一致。

## 输出

- 输出编解码实现的选择结论、回归清单与回退方式；不产出文件。

## 与相邻技能的边界

- 线路协议（Framed / TTHeader / gRPC）在 `kitex-transport-protocol-selection`；本 skill 只决定用哪种编解码实现。
- 降分配的目标如果是为了缓解 GC，先看 `kitex-allocation-hotspot-tuning`；GC 参数本身归 `go-runtime-tuning`。
- 本 skill 可独立调用，不依赖 `WORKFLOW.md` 的中间产物。

## 参考

- 协议选型：`kitex-transport-protocol-selection`
- 测量口径：`microservice-ab-testing`
