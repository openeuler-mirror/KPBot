# two-way-join — 双流 join（同 key 多事件）

两条有界流（orders / shipments，同 key 多事件）做 regular inner join：
`SELECT o.key, o.tag, s.label FROM orders o JOIN shipments s ON o.key =
s.key`。

## 语义锚定

- **regular join 终态语义**：对 insert-only 流，Flink 的 regular join 按
  key 双边缓存，对每个曾经共享 key 的 (order, shipment) 组合恰好发一行
  `+I`；不会在同类新事件到达时回撤旧对（inner 语义下旧的组合永远有
  效）。终态结果集即集合论意义下的内连接：同 key 的 orders ×
  shipments 全组合、重复计数保留。
- 单侧 key 不产出：key 3 只有 orders（delta/echo）、key 4 只有
  shipments（s-four），两侧共 0 行——钉死"join 丢侧"类 bug。
- 多事件同 key：key 1 有 2 orders × 2 shipments = 4 行——钉死
  "同 key 只留一行"的错误去重。
- 终态 5 行，全部 `+I`（本输入形态下无回撤；执行器的折叠逻辑对任意
  changelog 形态兜底）。

## 数据边界

- key 是首列，也是字段级归因的配对 key（同 key 行不唯一，归因配对按
  设计为尽力而为）。
- 字符串字段（tag/label）不含逗号、竖线、方括号。
