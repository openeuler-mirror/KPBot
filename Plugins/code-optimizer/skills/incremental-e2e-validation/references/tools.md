# 工具输入与验证

从技能目录执行，结果输出到标准输出，由调用方保存到实验目录。

```sh
python3 scripts/plan.py experiment.json
python3 scripts/plan.py pair.json --summarize
python3 -B -m unittest discover -s tests -v
```

矩阵输入中的数组顺序就是叠加顺序。每个 ID 唯一，依赖和互斥引用清单中的 ID。

```json
{"optimizations":[{"id":"A"},{"id":"B","requires":["A"]},{"id":"C","excludes":["B"]}]}
```

输出 configurations 中 reasons 非空代表该配置无法按当前清单执行。comparisons 的序号从零开始，rows 的三类字段引用相应序号，all_combined 引用最终累计比较。任一端配置不可执行时该比较应在报告中标明原因。

单对比较的描述性汇总输入如下，数值应为同场景、同指标、已核对条件的配对块结果。

```json
{"direction":"lower","blocks":[{"control":100,"candidate":90},{"control":102,"candidate":92}]}
```

每块输入值来自预声明统计方法，需在实验记录中保留块 ID、双方配置、运行日志和原始样本。此计算器只计算已提供数据的收益中位数、范围和逐块收益。范围表示观察值范围。verdict 始终要求人工或 Agent 核对条件、正确性、噪声与完整样本证据。最终 Markdown 表可由 report.py 生成，再由 Agent 按报告模板复核证据和补充解释。

非法结构或数值退出码为 2，成功输出为 0。框架采集命令和配置切换均在项目侧执行。
