# 精度域（app-tuner 侧）

跨架构、跨编译器、跨并行规模下的**计算结果一致性**问题：差异定位、兜住手段与验收口径。

## 边界判据

- **属于本域**：结论必须落在「浮点语义/编译变换/库实现/归约顺序」上才成立的问题——
  与 x86 结果对不上、精度回归、精度验收口径。
- **不属于本域**：
  - 模型推理结果的精度评测对比（准确率、答案回归题）→ `model-precision-analysis`（同插件，平铺）。
  - 性能优化本体（热点、向量化、并行化）→ `code-optimizer` 各阶段技能。
  - **只在 NPU 侧成立**（BF16/FP16 累加、HCCL 归约顺序、CANN/算子数值）→ 归昇腾采集/算子侧。
    注意 `ascend_kunpeng/` 域的边界判据同样排除 NPU-only 话题（它只收「NPU 与 CPU 必须合看才成立」的问题），
    所以不要把这类问题推给该域，避免两边互推形成路由真空。

## 与 `model-precision-analysis` 的分界

| 维度 | 本域 `kunpeng-precision-optimization` | `model-precision-analysis` |
|---|---|---|
| 输入产物 | 应用计算结果、编译选项、MPI 参数 | 两次推理评测的 JSON 结果 |
| 判定对象 | 浮点结果是否一致（RMSE/ME/MAE/相关系数） | 模型准确率是否回归（对→错题） |
| 触发词 | 结果对不上、`-ffp-compatibility`、Allreduce 顺序 | 准确率掉了、精度评测、回归题 |

## 成员

| Skill | 职责 |
|---|---|
| [`kunpeng-precision-optimization`](kunpeng-precision-optimization/SKILL.md) | 跨架构计算结果不一致的定位与精度调优：四层误差栈（代码语义/编译器变换/库实现/归约顺序）、精度优先选项集与 `-ffp-compatibility=18/21` 展开、ICC 循环重结合缺陷、归约算法统一与排序后归约、O0/O1 与选项加回定位法、插桩与接口扫描、四指标与 L0–L3 验收等级 |

## 域内路由

```
结果与 x86 对不上、要定位差异层  → kunpeng-precision-optimization（03 章决策树）
要选编译选项把精度拉回来        → kunpeng-precision-optimization（04 章）
多进程结果不一致                → kunpeng-precision-optimization（06 章）
要给「精度达标」下验收          → kunpeng-precision-optimization（02 章）
模型准确率是否回归              → model-precision-analysis
```

## 后续迁入 / 待议

- 模型侧精度技能（`model-precision-analysis`）是否并入本域统一「精度」入口：目前**不并入**——
  它的判定对象是模型准确率（对/错题），与本域的浮点结果一致性不是一回事；两边描述已去重（本域不再用「精度回归」当触发词）。
- 昇腾侧数值一致性（BF16/FP16 累加、HCCL 归约顺序）：归昇腾采集/算子侧，本域不登记、不反向引用。
