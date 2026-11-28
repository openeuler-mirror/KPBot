# agg-changelog — GROUP BY 聚合（count/sum）

对有界事件流做 `SELECT user_name, COUNT(*), SUM(amount) FROM events GROUP
BY user_name`。列名用 `user_name` 而非 `user`——**`user` 是 Flink SQL 解析器
的保留字**，不能作列名（真机提交时实证过：`ParseException: Encountered
"user"`），这本身就是该场景在真机上钉死的一个事实。作业运行期 print sink
看到的是完整 changelog：同 key 后续事件到达时逐次 `-U[旧值]/+U[新值]` 回撤
更新。

## 语义锚定

- **折叠语义**：终态结果集 = 每个 key 一行，`COUNT(*)` 计该 key 的事件
  数、`SUM(amount)` 计该 key 的金额和。执行器对捕获的 changelog 做同样
  的折叠（`+I/+U` 加入所携带的行、`-U/-D` 移除所携带的完整旧值）后与
  参考模型对比——丢回撤不会逃过终态对比。本场景的保证限定为**终态一致
  性**：print sink 协议下中间 changelog 更新不做独立校验（首次小计错、
  回撤携带同一错值、终态正确时仍判 PASS）；要校验中间态须用 retract
  感知的验证器。
- **类型口径**：Flink `COUNT(*)` 为 BIGINT、`SUM(BIGINT)` 为 BIGINT，
  print 输出均为普通十进制整数（`+I[alice, 3, 21]`）。
- 多事件同 key（alice×3、bob×2、carol×2）、单事件 key（dave×1）都在输
  入里；4 行终态。

## 数据边界

- 字符串字段（user 名）不含逗号、竖线、方括号。
- 金额全为正小整数，不涉 BIGINT 溢出与 null（`SUM` 空 key 组不会出现，
  每个 key 至少一行事件）。
