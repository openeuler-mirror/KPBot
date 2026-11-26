# 报告输入契约

## 命令

```sh
python3 scripts/plan.py experiment.json
python3 scripts/report.py results.json > report.md
```

两个工具使用相同 optimizations 数组生成矩阵。
结果文件增加 experiment 与 results。
每份文件描述一个场景、一个指标和一条路径。

## experiment 字段

| 字段 | 含义 |
|---|---|
| scenario | 场景名称 |
| baseline | 原版源码、构建和配置身份引用 |
| condition_id | 冻结条件清单标识 |
| conditions_log | 条件清单位置 |
| metric | 指标名称 |
| unit | 指标单位 |
| direction | lower 或 higher |
| performance_framework | 用户指定性能框架及版本 |
| correctness_framework | 用户指定正确性框架及版本 |
| minimum_blocks | 预声明的最少完整配对块数，整数且至少为 2 |

条件清单包含框架版本、命令、参数、输入及环境。
每次运行核对实际条件后再填写标识。
候选产物变化记录在 artifact_id。
额外条件变化需要新的 condition_id。

## results 字段

results 是以矩阵比较序号字符串为键的对象。
缺少的比较显示未运行。
非法配置显示依赖或互斥原因。
未知序号会导致校验失败。

status 可取 measured、not_run 或 failed。
后两者需要 reason。
measured 需要 direction、unit、blocks 和 environment_review。
environment_review 可取 stable、drift 或 unknown。
环境状态需有实际观测记录。

noise 可选，缺失时标明波动依据不足。
floor_pct 为非负有限数，表示经验噪声参考百分比。
evidence 指向数据及计算方法。
condition_id 与当前场景一致。

## blocks 字段

每块具有唯一 id、sequence、control 和 candidate。
sequence 可取 AB、BA、ABBA 或 BAAB。
A 表示该比较的控制配置，B 表示候选。
ABBA 的双方值为预声明方法得到的块内代表值。
每次原始运行均保存在日志中。
工具核对序列标签，真实执行顺序通过日志审查。

| 每侧字段 | 含义 |
|---|---|
| config | 矩阵配置 ID |
| active | 按清单顺序记录的实际启用项 |
| condition_id | 实际核对的冻结条件标识 |
| correctness | passed 才进入收益计算 |
| completed | 完整业务完成时为 true |
| hit | 已确认候选或原版路径生效时为 true |
| run_id | 运行或块内运行集合标识 |
| artifact_id | 实际运行产物标识 |
| log | 性能原始记录位置 |
| correctness_log | 正确性证据位置 |
| value | 有限正数，单位与指标一致 |

正确性证据需对应实际配置和产物。
相同配置的正确性记录可在各块复用。
输入、产物或覆盖变化时重新核对。

## 示例

以下为合成结构示例，一块会显示样本不足。

```json
{
  "optimizations": [{"id": "A"}],
  "experiment": {
    "scenario": "synthetic",
    "baseline": "base-build",
    "condition_id": "conditions-1",
    "conditions_log": "conditions.json",
    "metric": "duration",
    "unit": "ms",
    "direction": "lower",
    "performance_framework": "user-bench version-1",
    "correctness_framework": "user-tests version-1",
    "minimum_blocks": 3
  },
  "results": {
    "0": {
      "status": "measured",
      "direction": "lower",
      "unit": "ms",
      "environment_review": "stable",
      "blocks": [{
        "id": "block-1",
        "sequence": "AB",
        "control": {
          "config": "c0", "active": [],
          "condition_id": "conditions-1",
          "correctness": "passed", "completed": true, "hit": true,
          "run_id": "base-1", "artifact_id": "base-build",
          "log": "base-1.json", "correctness_log": "base-tests.log",
          "value": 100
        },
        "candidate": {
          "config": "c1", "active": ["A"],
          "condition_id": "conditions-1",
          "correctness": "passed", "completed": true, "hit": true,
          "run_id": "a-1", "artifact_id": "a-build",
          "log": "a-1.json", "correctness_log": "a-tests.log",
          "value": 90
        }
      }]
    }
  }
}
```

## 输出与校验

错误结构、未知比较、重复块及非法数值退出为 2。
配置或启用集合不匹配同样校验失败。
条件不可比、正确性不足、未完成和未命中写入结果格。
报告成功生成时退出为 0，可以含缺测和未确定状态。
工具读取记录声明，执行 Agent 仍需审阅真实日志。
原值显示两侧各自中位数。
收益显示逐块收益中位数，通常无法由两侧中位数直接复算。
报告后部提供逐块收益及日志引用。
