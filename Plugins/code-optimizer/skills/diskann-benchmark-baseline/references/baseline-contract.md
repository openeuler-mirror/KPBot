# DiskANN 基线契约

冻结源码 commit/版本/摘要、未提交补丁、编译器与公共 flags、Rust features 或 CMake 参数、二进制 hash、测试程序版本、数据/query/ground truth/保存索引 hash、有效查询参数和计时公式。

manifest 的 dataset.mode 为 memory 或 disk；兼容旧版缺省 disk。两者都记录 recall_at、search_list、线程与 CPU 资源；只有 disk 才要求 beam_width、is_flat、cache_nodes 和存储环境。NUMA 不适用时明确 null；非 Git 源码用 commit=unknown 加 source_digest=sha256:<digest>。validator 检查最小形状，不能证明 manifest 中的身份与运行事实一致。

索引构建和索引查询分别计时。比较查询实现时复用同一保存索引，不能在两侧各自重新随机建图后直接宣称实现收益。索引兼容性变化或算法/质量权衡另立实验。参数从实际 CLI 输出/机器可读结果核实；SSD 路径需要计数或 tracing 证明实际 I/O，不能凭延迟推断。

纯实现优化默认 Recall、结果语义、Comps、Hops 满足预先定义的相等或容差；SSD 另检查 IOs。内存路径 IOs 标为 not_applicable。正确性失败不得接收性能收益。

预先记录预热、page cache/设备状态、样本数、运行顺序、异常处理和漂移阈值；所有有效样本保留。冻结条件变化必须建立新 baseline_id。统计与跨负载汇总只消费 vector-experiment-methodology，不在此维护另一套规则。
