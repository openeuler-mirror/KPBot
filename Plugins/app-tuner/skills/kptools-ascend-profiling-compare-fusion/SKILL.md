---
name: kptools-ascend-profiling-compare-fusion
description: |
  专项对比：验证算子融合效果——融合前后算子数量变化、kernel 序列「N 个旧 kernel → 1 个新 kernel」
  的合并证据（三级证据等级）、kernel 时长与收益归因；证据不足时输出 unproven。
  当用户明确要看“融合有没有生效 / 合并了哪些算子 / 融合前后算子数量与耗时怎么变”时使用。
  通用“优化前后差多少 / 瓶颈变了吗”走 kptools-ascend-profiling-compare；
  算子集合与 shape 分布走 -compare-opstats；设备气泡走 -compare-bubble；
  单份 trace 分析走 kptools-ascend-profiling-analysis(-nostack)。
---

# 算子融合前后对比（fusion）

## 适用范围

- 融合前 / 融合后两份采集的 **kernel 序列对比**：数量、合并证据、时长归因。
- 不证明“融合是收益的全部来源”：间隙变化本批无法归因（标 `unsupported`）。

## 输入

- 两份采集目录（`*_ascend_pt` / `ASCEND_PROFILER_OUTPUT`）或裸 `trace_view.json`。
- 可选 `--fused-ops FILE`（每行一个融合算子名）：命中清单时把 `suspected` 提升为 `confirmed`。

数据源：`kernel_details.csv`（含 `Step Id / Name / Duration(us) / Start Time(us)`）优先；
缺失时用 trace 设备事件（`AI_CORE`/`AIV`/`MIX_*`/`hcom`）。

注：compare / compare-opstats / compare-bubble / compare-h2d-d2h / stream 与本 skill 为同批 PR 交付，合入前暂用 kptools-ascend-profiling-analysis / -analysis-nostack 承接。

## 工作流

```bash
python3 scripts/fusion_diff.py <base> <cand> --out-html report.html --json result.json \
  [--fused-ops fused.txt] [--window-tol-ms 0.05]
```

## 方法

1. 按 step 取 kernel 序列（按 `Start Time` 排序）；有 `ProfilerStep#N` 时，落在所有步窗之外的预热/收尾 kernel 排除并记录数量，不归入第 0 步。
2. 对两份序列做 LCS 对齐（按归一化名），得到公共子序列与缺口段。
3. 缺口段判定：
   - `k≥2 → m=1` 判 `merge`；`k=1 → m≥2` 判 `split`；其余 `change`（不计入证据）。
   - 证据等级：
     - `confirmed`：时间窗重叠 **且** 新名 tokens 覆盖所有旧名 tokens；或命中 `--fused-ops`。
     - `suspected`：时间窗重叠但名称无包含关系。
     - `unproven`：时间窗不重叠、缺 `Start Time` 无法验证时间窗，或一侧为空。
4. 收益归因：kernel 时长差（ms/step）+ 算子数量差（个/step）；**间隙 Δ 标 `unsupported`**。
5. 拆分（1→N）单独计数并在 `notes` 说明，不计入融合收益。

## 指标与口径

| 指标 | 单位 | 含义 |
|---|---|---|
| `fusion_op_count` | count | 每步 kernel 数（`info`） |
| `fusion_kernel_ms` | ms | 每步 kernel 总时长（Δ>noise → improved） |
| `fusion_merge_confirmed` | count | confirmed 合并组数（`info`） |
| `fusion_merge_suspected` | count | suspected 合并组数（`info`） |
| `fusion_merge_unproven` | count | unproven 缺口组数（`info`） |
| `fusion_attribution_kernel_ms` | ms | 收益归因的 kernel 时长分量（`info`） |
| `fusion_attribution_count` | count | 收益归因的算子数量分量（`info`） |

`Δ = base − cand`，正值表示候选更省；方向性指标与噪声只用**共同配对步骤**，噪声带
`max(--min-delta-ms, MAD(配对步骤序列))`；候选多采/少采的未配对 step 单列在 `notes`，不参与均值与噪声。

`confidence`：两侧 `kernel_details.csv` 正常 = `high`；trace 兜底 = `medium`；无 step 信息 = `low`。

## 输出

- 终端摘要、A.1 九字段 JSON、HTML（指标表、合并/拆分证据表、收益归因表含 `unsupported` 行、降级与口径）。

## 降级路径（始终产出报告）

1. 无 `kernel_details.csv` → trace 设备事件，`confidence=medium`。
2. 无 step 信息 → 整窗比较，`confidence=low`，`per_step` 只有 1 条。
3. 序列无法对齐（名称完全不同）→ 全部 `unproven`，`summary` 写「无法证明融合」。
4. 反向场景（拆分）→ 记 `split`，`summary` 说明拆分，不算融合收益。
5. 序列过长（>1500）→ 放弃 LCS，只做数量/时长对比并把缺口全部标 `unproven`。
6. `kernel_details.csv` 缺 `Start Time` → 保留缺测状态，数量/时长照常统计，但合并/拆分的时间窗证据标 `unproven`（不把起点当 0 制造假重叠）。
7. 输入不可读 → HTML 错误页 + 退出码 3。

## 真实数据口径

- 对齐归一化会去掉**数字实例后缀**（`hcom_x_98_1` 与 `hcom_x_293_1` 视为同一类），避免把实例后缀差异误判成拆分/合并。
- 设备任务用 `args["Task Type"]`（`cat=None`）；时间单位 µs；`ProfilerStep#N` 只按名字识别。
- 实测（vLLM nomal vs eager）：0 处合并、0 处拆分，结论「无法证明融合」，符合预期。

## 限制

- 「间隙 Δ」需要 host 下发数据（T1-2，本批不做），显式 `unsupported`，不用别的方式凑数。
- 名称 token 覆盖只是启发式证据，不等价于图级融合证明。
- 跨采集不做时间戳对齐，只在各自采集内比较。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_fusion -v
```
