# Top-N / 去重算子族语义对照（终态矩阵模板）

本文是 `flink-native-stateful-operator-tuning` 的语义对照参考：改码前把**原生
实现规格**与**向量化实现行为**逐项对照，裁决每项差异是"已知差距（可接受）"还是
"必须修复"。本表是模板（以 Top-N/ROW_NUMBER 去重族为样例），其他算子族按同结构
扩展。锚点用类名（Flink 原生侧 grep `flink-table` 可定位；向量化侧按开源 PR 分支
grep）。

## 原生规格基线（对照的锚）

原生实现（AbstractTopNFunction 函数族）的两级分派：

- **第一级——策略**：按输入 changelog 形态选处理策略，实现类四个
  （Undefined / AppendFast / UpdateFast / Retract，`RankProcessStrategy`）；
- **第二级——函数**：策略 × sortFields 结构 × Top-1 特例分派到五个函数
  （AppendOnlyFirstNFunction / FastTop1Function / AppendOnlyTopNFunction /
  UpdatableTopNFunction / RetractableTopNFunction）。

每类函数规格覆盖：状态结构（ValueState 存 Top-N 数据、MapState 存 sortKey→数据）、
比较器生成（Janino 生成 GeneratedRecordComparator）、LRU 缓存口径
（table.exec.rank.topn-cache-size 配置）、changelog 发射节奏（逐行更新）、以及
去重特例（rank≤1 时按处理时间/事件时间与保留首行/末行选路——具体函数类七个：
ProcTime KeepFirst/KeepLast 及其 MiniBatch 变体四个，加 RowTime 侧三个；
RowTime 的首行/末行折叠在 RowTimeDeduplicateFunction 的 keepLastRow 布尔分支；
含 MiniBatch 轴则展开为八条选路路径）。

## 语义对照矩阵（样例条目）

对照维度与判定风格如下；实际矩阵按算子族逐项展开。

| # | 维度 | 原生规格 | 向量化实现终态 | 判定 |
|---|---|---|---|---|
| 1 | 排名语义 | 仅 ROW_NUMBER 流上可用；RANK()/DENSE_RANK() 抛异常 | 同口径（不支持即 fail loud） | 对齐 |
| 2 | Top-N 条数 | rankRange **起止名次区间**全语义（Top-1/First-N/Top-N，以及 `rn BETWEEN m AND n` 类区间——上界与下界是两个独立语义） | 上界经 limit 字段传递；下界过滤由 rank 之后的 filter/calc 节点执行（SQL WHERE 条件独立成计划节点，翻译保留）。历史上曾有硬编码 Top-1 时期，属已修复差距 | 对齐（验证项：**上界传递与下界过滤分别验收**——须用起始名次大于 1 的查询验收下界，防止"第 6-10 名与前 10 名上界相同"而仅验 limit 导致误判对齐） |
| 3 | 输入形态 | 策略四选一（Undefined/AppendFast/UpdateFast/Retract），覆盖 append/upsert/retract | 翻译时忽略策略，执行层按输入 rowKind 分支处理 | 对齐机制不同——验证 update-before 发射 |
| 4 | changelog 节奏 | 每行进榜即发更新（每行一次） | 每批结算一次的批节奏 + 行级 rowKind 传递 | 已知差异：折叠后终态一致，节奏不同 |
| 5 | 去重特例 | 处理时间/事件时间 × 首行/末行选路（七个函数类；含 MiniBatch 轴为八条路径） | 事件时间路径完整；处理时间路径不支持即显式抛异常 | 部分对齐：不支持项 fail loud，不静默错算 |
| 6 | LRU 缓存 | topn-cache-size 有界缓存 | 有界 LRU 对齐原生口径（历史上曾传入即弃，属已修复差距） | 对齐（验证项） |
| 7 | rowKind 保真 | 全链路传递 | 批尾 $row_kind 列契约传递，跨算子经出/入桥保留 | 对齐（回归项） |

## 差距处置纪律

1. **每项差距三选一**：修复（进实施计划）/ 已知差距（记录触发条件与影响面，fail
   loud 或文档标注）/ 不支持（显式抛异常）——**禁止静默错算**；
2. **节奏差异与终态差异分开裁决**：changelog 发射节奏可以不同（批结算 vs 行结算），
   但折叠回终态结果集必须精确一致——这是行为冻结核验的判定对象，不是可选项；
3. **回归清单从矩阵来**：矩阵中所有"验证项/回归项"进入分阶段门禁的核验用例
   （Key 分组 changelog 折叠对比 + 多并行 + savepoint 续跑）；
4. **版本演进史不入矩阵**：中间版本的差距（如历史上硬编码 Top-1 时期）只在归档
   记录中保留，矩阵只对照终态——读者不需要知道中间态。

## 其他算子族的扩展要点

- 聚合族：两阶段聚合（local/global accumulate→merge）语义、窗口触发顺序
  （事件时间随水位线推进）、TTL 清理对照；
- Join 族：probe/build 侧角色、状态驻留生命周期、changelog 修正语义对照；
- 扩展时复用同一结构：原生规格锚（类名族）→ 对照维度 → 判定三选一 → 回归清单。
