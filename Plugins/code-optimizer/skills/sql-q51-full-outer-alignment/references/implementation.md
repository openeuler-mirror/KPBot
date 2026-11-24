# 全外连接与双渠道对齐

先衡量按 `(item_sk,d_date)` 的双侧 Shuffle 和外连接内存，分别记录匹配、仅 Web、仅门店的行数。测当前连接策略的输出和 spill，并检查后置窗口是否因为重新分区而增加排序。可选改动包括缩减输入列、利用可证明的分区契约，或改用当前引擎已经支持且语义等价的外连接路径。

允许 SQL 重写时，可将**分支累计完成之后**的两路结果做 `UNION ALL`，通过分组对齐得到两列渠道累计值。应用前证明两个分支各自对于 `(item_sk,d_date)` 最多一行，且两个键均非空。任一连接键可能为空时，保留原外连接直到能证明等价；金额列为 NULL 则仍按原义保留。`GROUP BY` 会将相同 NULL 键合并，普通 `=` 外连接不会。重复键时，外连接可形成多重匹配，分组会压平重数。

对已证明的唯一非空键，按以下形状验证 plan。这是类型参数化示意，`<store_type>`/`<web_type>` 必须替换成实际分支的输出类型后才执行，不能原样运行。两个 `NULL` 分别显式转为对应渠道列的类型。保留两路原始累计列及后面的累计 `MAX`、条件过滤和排序。

```sql
SELECT item_sk, d_date, MAX(web_value) AS web_sales,
       MAX(store_value) AS store_sales
FROM (
  SELECT item_sk, d_date, cume_sales AS web_value,
         CAST(NULL AS <store_type>) AS store_value FROM web_v1
  UNION ALL
  SELECT item_sk, d_date, CAST(NULL AS <web_type>), cume_sales FROM store_v1
) channels
GROUP BY item_sk, d_date
```

这里每侧至多一个值，`MAX` 用于取回该值。渠道缺席或其当日累计值为 NULL 时，输出保持 NULL。只有实测 Shuffle、spill 和总耗时改善才保留该重写。

## 源码定位

### 准入、反例和原路径

| 条件 | 证明或反例 | 未证明时 |
| --- | --- | --- |
| 两侧各自键唯一 | 按两列检查，不仅看哈希表某侧 | 原全外连接 |
| 键均非空 | NULL/NULL 在普通 `=` 连接中各自保留，分组却合并 | 原全外连接 |
| SQL 重写允许 | 沿用任务选择的改动层级 | 只分析或改已授权的执行实现 |
| 输出与状态类型兼容 | 两路 NULL 的类型、精度、比较和额外谓词 | 原输出/连接路径 |

仅 Web 一侧有两行同键，门店一行时，外连接产生两行，按键分组对齐通常只剩一行；不需要双侧都重复才失败。已有 NULL 金额的记录不是缺失记录，不能过滤 `cume_sales IS NULL` 来判断渠道存在。

### 分层结果比较

先比较完整对齐行多重集，再比较两列后置 MAX，最后比较过滤和 Top 100。缺侧原始列继续为 NULL，即使其后置历史峰值非 NULL；累计下降时不能用最后非 NULL 值替代 MAX。测试单侧空、双侧空、同键全 NULL 和负金额，以结果行数与值共同验收。

测量将匹配输出、两侧独有输出、总输出和双方输入分开，匹配行数按实际输出重数计，不按 distinct 键数估算。记录替代路线新增的聚合/排序/交换成本；真实 SQL 的比较器和 decimal 验证独立于 SQLite 模型。

从 Spark 最终物理计划确认连接实现与 Gluten 下推决策。实际命中 Velox 哈希连接时，检查 `HashProbe.cpp` 的外连接输出路径和 spill 指标。使用 SQL 重写时，以聚合替代路径的最终计划和键守卫为准。

## 查询来源

KPBench 中的 SQL51 模板位于 velox/velox/src/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries/q51.sql，SHA-256 为 ecc8b4945d07da827dc36414fbc94040eff22f6b542ef090c13e3b2dea754e17。执行和统计以用户实际查询、目标版本与最终物理计划为准。

没有实测证明该重写一定减少 Shuffle 或 spill；这些是需要前后采集的指标。模板和文件名是源码定位线索，不是机器路径依赖或固定运行参数。
