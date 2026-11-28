# filter-project — 谓词过滤 + 投影 + 表达式运算

对有界 item 流执行 `SELECT id, name, price * qty, qty + 1 FROM items
WHERE price > 100 AND qty >= 2`。

## 语义锚定

- **谓词边界**：输入行专门钉死两个边界——`price = 100` **不**入选（严
  格 `>`），`qty = 2` **入**选（`>=`）。错用 `>=`/`>` 会立刻在行集上
  体现（多出 2002 行或丢掉 2006/2007 行）。
- **表达式类型口径**：Flink 中 `BIGINT * INT -> BIGINT`、`INT + INT ->
  INT`，print 输出均为普通整数。乘加语义是本场景直接检验的对象（下推
  改写最容易在这里动表达式）。
- 纯 INSERT-only 无 changelog 折叠负担，作为过滤/投影路径的最小快速
  检验。

## 数据边界

- 行首 `id` 唯一，作为字段级归因的配对 key。
- 字符串字段（name）不含逗号、竖线、方括号。
- 8 行输入 4 行过谓词。
