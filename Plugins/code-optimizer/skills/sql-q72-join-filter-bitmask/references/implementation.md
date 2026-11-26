# 内哈希连接过滤输出压缩的源码定位

来源为用户 SQL72 包中的实施摘要，本次本地整理未复跑引擎。`src/velox/` 是目标工程的包装路径，候选分支可能属于业务补丁；应补齐实际仓库、完整提交、补丁和原始日志后再复现。以下历史数据不代表本轮成绩。

## 查询来源

已读取 KPBench 根目录下 `velox/velox/src/gluten/tools/gluten-it/common/src/main/resources/tpcds-queries/q72.sql`，SHA-256 为 `186d7f086c498743f59c26f6fef950f7ecc06f3b4dfc962e11be915a751766bd`。模板商品连接后的数量条件可以产生过滤配对，但不能证明当前引擎采用此位图分支。路径仅作来源线索，实际调用与过滤布局由目标计划、源码和运行记录确认。

## 调用链与数据结构

SQL72 的库存与销售内连接按 `item_sk` 查表，建表侧同商品可能有多行。`listJoinResults` 枚举候选配对，`HashProbe::evalFilter` 根据剩余条件生成并解码布尔结果。快路径替换该函数末段的行映射压缩，保留谓词计划和输入。

| 源码入口或字段 | 需要核对 |
| --- | --- |
| `src/velox/velox/exec/HashProbe.cpp` 的 `evalFilter` | 保留原逻辑分派，新分支位于特殊连接处理之后、通用 `filterPassed` 之前 |
| `filterInputRows_` / `decodedFilterResult_` | 全选、解码为 identity、无 NULL 时才能读取 `data<uint64_t>()` |
| `bits::forEachSetBit` | 以 `[0, numRows)` 正序遍历，处理最后一个字的越界填充位 |
| `outputTableRows` / `rawOutputProbeRowMapping` | 同一通过位写同一输出槽，推进 `numPassed` 后用于后续列提取 |

原地压缩证明使用第 `k` 个真位下标 `i` 满足 `k <= i`。已读取源 `i` 的两组映射后写目标 `k`，不会覆盖下一真位对应的尚未读取源。两个不同配对即便指向同一个建表行或同一探测行，也各占一个输出槽。

## 验收边界与实测

对布尔 `[false, true, false, true]`，使用不同的建表指针和探测行号，确认输出是原下标 `1、3` 的一一对应组合。另用 65 行和脏尾位检验仅按候选域读。带 NULL、字典/常量映射、部分选择或非内连接时，必须由引擎原语义处理。

第 2 轮提交 `1fc8b6e` 相对冻结基线仅修改该分支。官方三对查询中位耗时 `99.677 → 92.806` 秒，加速比 `1.0740×`。目标端压缩命中率和局部成本仍应另测，分清配对总数、压缩通过行数和求值次数。
