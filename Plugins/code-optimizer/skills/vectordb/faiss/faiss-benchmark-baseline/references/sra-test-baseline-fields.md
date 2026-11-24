# 测试字段映射到基线

每个工作负载记录：源码/构建/库身份、数据与 ground truth、索引文件、metric/预处理、全部有效参数、线程/CPU/NUMA、运行时覆盖值，以及原始指标字段/单位/公式。
QPS 可能按 wall time、平均 thread time 或其他聚合计算，必须从当前计时代码确认。保留每次循环和最终统计，Recall 按当前 ground truth 与 top-k 口径记录。
数据集矩阵由任务声明，以显式数据集 ID 对齐结果，不依赖某份 test.sh 的固定顺序。默认参数不能从另一个算法或副本继承；缺失值与不适用值分别说明。
输出解析方法见 `faiss-sra-test/references/sra-test-metrics-and-output.md`。
