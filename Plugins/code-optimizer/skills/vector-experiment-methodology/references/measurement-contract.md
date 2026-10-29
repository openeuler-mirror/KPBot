# 向量检索性能实验契约

## 目录

1. 冻结维度
2. 运行顺序
3. 正确性门禁
4. 统计报告
5. 机器可读输入
6. 验收检查表

## 1. 冻结维度

运行 A 或 B 前记录：

| 分组 | 必填字段 |
| --- | --- |
| Source | repository、baseline commit、candidate commit、patch/feature |
| Build | 编译器及版本、目标 CPU/ISA、优化 flags、link mode、feature list |
| Machine | CPU 型号、socket/core/SMT、NUMA 绑定、内存策略、kernel、governor |
| Storage | 设备、必要时记录 firmware、文件系统、mount options；布局相关时记录索引 extents |
| Dataset | 名称、N、D、数据类型、query 数、ground truth、索引身份 |
| Search | distance、top-k/recall-at、L/nprobe/efSearch、beam、线程数、cache nodes、flat/refine |
| Run | warmup、page-cache 策略、顺序、重复次数、竞争负载 |

任何冻结字段变化时，都应将结果标记为新实验，而不是原 A/B 的延续。

## 2. 运行顺序

优先使用 A1-B1-B2-A2 或更长的平衡序列。A 是冻结 baseline，B 是 candidate。第二个 A 用于检测温度、后台负载、SSD 状态或 cache 演化引起的漂移。

对于运行代价高的大数据集，只有全部契约 hash 匹配且小型 sentinel 数据集未显示显著机器漂移时，才可复用先前冻结基线，并必须声明该限制。

## 3. 正确性门禁

纯实现优化默认要求以下不变量精确一致：

- 数据集和 query 顺序一致。
- Recall 使用相同 ground truth 和公式。
- 除非改动明确改变查询语义，否则 DiskANN IOs、Comps 和 Hops 一致。
- Faiss result ID/distance 和算法工作量 counter 满足预设容差。

浮点重排可能需要声明容差。必须在运行前设置，看到失败后不得静默放宽。

## 4. 统计报告

每个数据集报告：

- A 和 B 的 throughput 或 latency 中位数。
- throughput 越高越好时 speedup=`B/A`；latency 越低越好时 speedup=`A/B`。
- 至少三个样本时报告 min/max 或 MAD。
- 正确性差异。

跨数据集报告几何平均：

`geomean = exp(sum(log(speedup_i)) / n)`

不得对不同数据集的原始 QPS 求算术平均。

## 5. 机器可读输入

`scripts/compare_results.py` 接受（以下为合成字段示例，不代表测量或推荐配置；实际矩阵和指标写入 manifest）：

```json
{
  "performance_field": "qps",
  "higher_is_better": true,
  "correctness_fields": ["recall", "ios", "comps", "hops"],
  "datasets": {
    "<dataset-a>": {
      "baseline": [
        {"qps": 1000.0, "recall": 95.00, "ios": 400.0, "comps": 12000, "hops": 100}
      ],
      "candidate": [
        {"qps": 1010.0, "recall": 95.00, "ios": 400.0, "comps": 12000, "hops": 100}
      ]
    }
  }
}
```

脚本只负责描述性计算；最终接受仍取决于实验契约和 Reviewer 判断。

每条性能样本都必须包含有限数值，布尔值、缺失值、NaN 和 Infinity 均报错，不会静默丢弃后继续计算。正确性字段在任一样本中缺失或无效时标记为 `available=false`，不能据此声称质量已通过；输出只包含标准 JSON 可表示的数值。

## 6. 验收检查表

- [ ] 契约字段一致。
- [ ] Diff 只包含一个主要假设并提供 disable switch。
- [ ] 正确性 smoke 通过。
- [ ] 运行顺序能够检测漂移。
- [ ] 包含所有有效样本。
- [ ] 报告正确性差异。
- [ ] 报告各数据集及 geomean 性能。
- [ ] 归档原始日志、命令、commits 和环境。
- [ ] 记录 failed 和 neutral 候选。
