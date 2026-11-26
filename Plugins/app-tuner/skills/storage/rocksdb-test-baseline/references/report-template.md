# RocksDB Baseline Report Template

## 1. 测试信息

- 测试时间
- hostname
- profile
- 结果目录

## 2. 环境

| 项 | 值 |
|---|---|
| CPU 架构 | |
| CPU 型号 | |
| CPU 列表 | |
| NUMA node | |
| OS | |
| kernel | |
| RocksDB 版本 | |
| RocksDB 构建 flags | |
| YCSB commit | |
| gcc | |
| Java | |
| Maven | |

## 3. 内存

- 单 NUMA node size
- 单 NUMA node free
- 系统内存总量/可用量
- HugePages
- Transparent HugePages
- load 前内存检查值
- drop pagecache 后内存检查值

## 4. 磁盘

- 数据目录所在设备
- 磁盘型号
- 磁盘类型/ROTA
- 文件系统
- 挂载点和挂载选项
- 可用空间
- 数据集大小

## 5. 参数

- recordcount
- operationcount
- fieldlength / fieldcount
- load / warmup / bench threads
- warmup 每轮 operationcount
- workloads

## 6. Warmup

- 轮数
- 每轮当前 LOG 的 compaction_started / compaction_finished
- 最后一轮结果

## 7. Workload 结果

| workload | throughput(ops/s) | operations | return_errors |
|---|---|---|---|
| a | | | |
| b | | | |
| c | | | |
| d | | | |
| e | | | |
| f | | | |

## 7.1 分操作延迟

按 YCSB 实际操作类型分别报告，避免混合 workload 的 P95/P99 被最大值高估。

| workload | operation | operations | avg(us) | p95(us) | p99(us) |
|---|---|---|---|---|---|
| | READ | | | | |
| | UPDATE | | | | |
| | INSERT | | | | |
| | SCAN | | | | |

CLEANUP 属于线程清理指标，不纳入业务 operations 和平均延迟。

## 8. 数据备份

- 数据目录大小
- 备份路径
- 备份状态
- 是否保留原数据目录

## 9. 结论

- 是否跑完六个 workload
- 是否存在失败阶段
- 该结果就是当前平台的基线,不做阈值判定
