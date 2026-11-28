# datetime-expr — 10 个时间提取函数投影

对有界 bid 流投影 `YEAR/MONTH/DAYOFMONTH/DAYOFYEAR/DAYOFWEEK/QUARTER/
HOUR/MINUTE/SECOND/WEEK` 十个时间提取函数，检验受测栈对 Flink SQL 时间
语义的实现。输入缩编自 GlutenFlink e2e nexmark bid 数据（8 行确定性集合，覆盖年界/
季界/周界/周内各天/午夜等边界）。

## 语义锚定

**时区约定（本场景最核心的锚）**：

- Flink 1.19 CSV 格式对 `TIMESTAMP(3)` 只接受无时区偏移的字面量
  （LocalDateTime / SQL 格式 `yyyy-MM-dd HH:mm:ss[.SSS]`）。GlutenFlink e2e 曾
  为此把 C++ dump 的 `…Z` ISO 形式先转成 SQL 格式，native Flink 才读得
  进去。本场景的输入时间戳全部按此格式书写。
- 本场景把输入时间戳定为 **UTC 挂钟时间 + 固定偏移 0**：参考模型按无
  时区的 naive datetime 解析、直接抽取各分量，不施加任何偏移。
- 这样锚定的依据：Flink 的 `TIMESTAMP`（不带时区）在**解析**与**分量
  提取**两处都经过 session timezone，两次转换相互抵消——分量值恒等于
  CSV 写下的挂钟分量，与集群 session timezone 是什么无关（GlutenFlink e2e 的
  native 基线在 `-Duser.timezone=Asia/Shanghai` 集群上读 UTC 挂钟输入，
  十列中九列与 Python 按 UTC 计算的 ground truth 逐值一致，实证了这一
  往返不变性）。
- 因此**任何一侧破坏该不变性都会在本场景 FAIL**：GlutenFlink 历史上 velox 的
  `InitSessionTimezone` 默认 UTC、不继承 Flink 的
  `-Duser.timezone=Asia/Shanghai`，解析与提取各说各话，正是这类 bug。
  这不是本场景的误报条件，而是它的存在目的。

**DAYOFWEEK**：Flink/SQL 标准口径 **Sun=1..Sat=7**（`EXTRACT(DOW)` 同
口径）。GlutenFlink e2e 曾据此暴露 velox sparksql `DayOfWeekFunction` 的真 bug：
它直接返回 `tm_wday`（Sun=0..Sat=6），周三 2026-07-22 velox 印 3、
native 印 4——9200 行全列失配。本场景输入含周日（2025-12-28）与周六
（2026-10-31），参考输出分别给 1 和 7，钉死该口径。

**WEEK**：Flink 文档口径为 ISO 8601 周号（第 1 周含当年首个周四），
Python `date.isocalendar()[1]` 与之完全一致（GlutenFlink e2e native 基线 wk 列与
该算法 9200/9200 行一致）。输入含跨 ISO 年边界样例：2027-01-01（周五）
属 2026 年第 53 周——顺带钉死"周号属于哪个 ISO 年"的规则。

**QUARTER**：`(month - 1) // 3 + 1`。GlutenFlink 历史上 gluten-flink 把
`QUARTER(x)`/`WEEK(x)` 归一化成 `EXTRACT(FLAG(QUARTER)/FLAG(WEEK), x)`
RexCall 路由到 velox 的 extract UDF，该 UDF 只实现 10 字段中的 8 个，
两列全列 null——本场景因此保留这两列。

**返回类型口径（真机验证钉死）**：Flink 1.19 中 `YEAR(dt)`…`WEEK(dt)`
十个函数的返回类型均为 **BIGINT** 而非 INT（真机提交的 query schema
逐列实证），sink DDL 以 BIGINT 承接；print 输出与 INT 无格式差异，参考
模型按普通十进制整数串产出即可。

## 数据边界

- 行首 `id` 唯一，作为字段级归因的配对 key。
- 时间戳毫秒位全部非零且不以 0 结尾（规避 native Flink 对 `.000` 的
  序列化裁剪差异；本场景不投影 dt 本身，此为数据卫生而非硬约束）。
- 字符串字段不含逗号、竖线、方括号（print 行字段边界约束）。
