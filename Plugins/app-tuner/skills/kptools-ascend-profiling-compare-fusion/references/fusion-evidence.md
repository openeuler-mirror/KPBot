# 融合证据与归因规则

## 1. 序列对齐

- 按 step 取 kernel 序列，先按 `Start Time(us)` 排序；无 `Start Time` 时按文件顺序（写入降级说明）。
- `kernel_details.csv` 缺 `Step Id` 时按整窗（单组）处理，写入降级并将置信度降为 `low`（与 trace 无 `ProfilerStep#N` 一致）。
- 名称归一化：小写 + 去除仅作为实例编号的尾部数字组（如 `_98_1`）+ 去除非字母数字；名称主体里的数字保留，因此 `Conv2D` 与 `Conv3D` 不会被合并。大小写/分隔符变体（`MatMul_v2` 与 `matmulV2`）归一化后视为同一算子。
- LCS 只用于找「公共子序列」，不对缺口做惩罚；缺口段就是候选变化区。
- 序列长度 > 1500 时放弃 LCS（内存/时间），改走「只比数量与总时长」的降级路径。

## 2. 缺口分类

| 旧段 | 新段 | 分类 | 是否计入融合证据 |
|---|---|---|---|
| k ≥ 2 | m = 1 | `merge` | 是 |
| k = 1 | m ≥ 2 | `split` | 否（单列统计） |
| 其他 | | `change` | 否 |

## 3. 证据等级

| 等级 | 判据 |
|---|---|
| `confirmed` | 时间窗重叠（±`--window-tol-ms`）且新名 tokens 覆盖**每个**旧名的至少一个 token；或新名出现在 `--fused-ops` 清单 |
| `suspected` | 时间窗重叠，但名称无明确包含关系 |
| `unproven` | 时间窗不重叠 / 缺失 Start Time 无法验证时间窗 / 一侧为空 / 序列过长无法对齐 |

时间窗：旧段 = 第一旧 kernel 起点到最后一个旧 kernel 终点；新段同理；重叠判定带容差。

命名包含用 token 集合（驼峰拆分 + 去纯数字 token + 去 token 尾数字），
例：`MatMul`+`BiasAdd`+`Relu` → `MatMul_BiasAdd_Relu` 命中 confirmed。

## 4. 收益归因

| 分量 | 计算 | 本批状态 |
|---|---|---|
| kernel 时长 | `Σbase dur/step − Σcand dur/step`，ms | 可用 |
| 算子数量 | `base 每步个数 − cand 每步个数` | 可用 |
| 间隙 Δ | 需要 host 下发/设备启动间隔 | **unsupported** |

间隙 Δ 不可用时会虚报收益（把「更碎但间隙更小」误当融合收益），所以显式留空。

## 5. 结论口径

- 有 confirmed/suspected 合并 → 报告合并组数与时长/数量变化。
- 只有拆分 → 明确写「未发现融合证据（检测到 N 处拆分）」，不输出收益结论。
- 什么都没有 → 「无法证明融合」。

## 6. 边界

- 算子集合与 shape 分布：`kptools-ascend-profiling-compare-opstats`。
- 步耗时与四分类：`kptools-ascend-profiling-compare`。
- 设备气泡：`kptools-ascend-profiling-compare-bubble`。
