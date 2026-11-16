# DiskANN 实验验收

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 读取 [acceptance-gates.md](acceptance-gates.md) 和共享的 `vector-experiment-methodology` 契约。
2. 在 CP7 实验验收中，根据实现说明和最终 diff 核对改动范围；不要把这一步变成默认的 CP4 逐行代码审查。
3. 验证 feature-off 在声明噪声内复现冻结基线。
4. 复跑或检查独立的正确性与性能证据。
5. 检查每个数据集，而不只看几何平均；解释离群点和负收益案例。
6. 给出一个结论：`ACCEPT`、`ACCEPT_WITH_NOTES`、`REWORK`、`REJECT` 或 `INCONCLUSIVE`。
7. 追加验收发现，不得编辑算法代码。

## 必需报告

- candidate 与 baseline commit；
- 假设及影响的源码路径；
- 契约匹配状态；
- 正确性表；
- 各数据集原始样本与收益；
- 漂移/噪声分析和 geomean；
- 风险、限制及复现步骤；
- 明确列出未通过门禁的 verdict。

## 按需资源

- [acceptance-gates.md](acceptance-gates.md)：执行正确性、统计、复现和归档门禁时读取。
