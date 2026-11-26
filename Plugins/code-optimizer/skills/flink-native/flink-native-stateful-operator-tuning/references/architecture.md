# 向量化栈三层架构与替换边界

本文是 `flink-native-stateful-operator-tuning` 的架构参考：改码前必须知道**哪些环节
属于向量化栈（可以改）、哪些环节留在 Flink（改了会破坏语义）**。锚点一律用类名
（grep 可定位），行号随版本漂移不引用。

## 三层栈

| 层 | 职责 | 关键机制 |
|---|---|---|
| planner 翻译层（gluten-flink 模块） | 把 Flink 执行计划翻译成 native 计划 | 影子 ExecNode：与 Flink 同名 ExecNode 平行的翻译类，忽略 Flink 侧策略选择，构造计划节点树（如 TopN 类节点 → 流式 keyed 壳节点 → StatefulPlanNode），以 JSON 一次过 JNI |
| 跨语言桥（velox4j） | Java↔C++ 数据与控制通道 | JVM 壳算子（GlutenOneInputOperator 类）挂回 Flink Transformation；计划 JSON、数据队列、输出回流、checkpoint 快照直通均经此层 |
| native 执行层（velox） | 计划执行 | StatefulTask 拉取式单线程推进；行式 RowData 逐条转 1 行列向量入队，引擎内部以向量批为单元推进；keyed 算子按分区键切批逐 key 处理 |

## 替换边界（什么被换、什么没换）

**被替换的两段**：

1. **翻译段**：Flink 的算子策略选择 + Janino 代码生成（比较器/相等器生成）被替换为
   静态计划节点构造——排序规格直接映射为引擎排序描述，不做运行时 Java 代码生成。
2. **执行段**：逐行 `processElement` + keyed state 循环被替换为"逐行过桥 + 引擎内
   向量化批处理"的混合模型；状态值统一为向量引用（这正是状态形态审计的对象）。

**未被替换（留在 Flink）的**：SQL 解析与优化（Calcite）、StreamGraph/JobGraph 生成
与调度、barrier 对齐与 checkpoint 协调、sink 提交、以及 keyed state 的 key selector
注册（分区键仍注册进 Flink Transformation，供状态直通与语义对齐）。

**checkpoint 混合模式**：barrier 仍由 Flink 驱动；算子 snapshotState 经 JNI 沿 native
链快照——heap 后端显式快照回 Java 侧 Flink state store；RocksDB 后端共享 Flink 活
实例（native 句柄传入）。改状态形态时必须同步核对这条链。

## 翻译分叉点

分叉发生在 ExecNodeGraph → Transformations 阶段：影子翻译类把 ExecNode 转成
native 计划节点 + JVM 壳算子（挂回同样的 Transformation）；非 native 化的算子继续
走 Flink 原生 translate。**每个向量化算子独占一个算子链切片**（链化约束：JVM 壳
算子与普通算子不能同链自由合并）。

作业图卸载由 OffloadedJobGraphGenerator 类机制裁决：整段连续的 native 算子合并进
一个 vertex；**两个 native vertex 之间仍是普通网络边**（见 transport-wire.md）。

## 数据面：push-then-pump 事件泵

一条记录的完整环：

```text
上游逐条输出 RowData（JVM 壳算子每条一次 processElement）
  → 转 1 行列向量（行尾补 row_kind 列）
  → 入阻塞队列（Java 侧不攒批）
  → C++ 侧拉取式消费（ExternalStreamDataSource 类的 next 接口）
  → 引擎内逐算子推进（向量化批）
  → 输出向量批 → 回流 Java → 逐行还原 RowData
```

攒批事实：**source 端整批到达（数据源天然成批）；中间向量化算子零攒批（来一批处理
一批）；批大小的决定因素是桥上的攒批配置（如 preferred_output_batch_rows 类配置键）
与 source 批形态**。跨语言边的"小批固定开销"由此产生（见姊妹 skill 传输税）。

零拷贝的三种真相：同 vertex 内两个向量化算子之间——整批向量直通（真零拷贝）；
Java↔C++ 桥——Arrow 缓冲驻留 native 内存、Java 只持引用（桥内零搬运）；跨 vertex
网络边——必须序列化（零拷贝不存在，见 transport-wire.md）。

## 时间语义在 native 侧的映射

| 概念 | native 侧机制 |
|---|---|
| watermark | 旁路控制信号：processWatermark → task.notifyWatermark，与记录、timer 处理经 mailbox 严格串行 |
| 事件时间 timer | registerEventTimeTimer → 只随 advanceWatermark（水位线推进）触发——必须有 watermark |
| 处理时间 timer | registerProcessingTimeTimer → 处理时间调度器按墙钟触发，回调经 Java mailbox 回流——全程无 watermark 参与 |
| 事件时间去重 | 用数据内 rowtime 字段比较新旧（前一行 ValueState），不等 watermark |

两条 timer 路径的割裂（事件时间随水位线 / 处理时间随墙钟）是"watermark 专属事件
时间"在向量化栈的工程印证。Top-N 类无窗算子没有"到齐"语义——删掉 watermark 声明
输出不变，可用于判定查询是否真的依赖 watermark。

## 调用本参考的判定

- 新增算子翻译前：先确认它属于"被替换两段"的哪一段，keyed 语义是否需要 key
  selector 注册回 Flink（参考 plan-node-contract.md 的握手）。
- 疑似架构级问题时：核对"未被替换"清单——问题出现在 Calcite/调度/barrier 段，
  向量化栈不是嫌疑人；问题在翻译/执行段，按本文件机制图定位层。
