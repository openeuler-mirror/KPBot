# 负结果档案

> 配套 SKILL.md。负结果与正结果同等固化——防止重复踩坑，且同一优化换个形态可能翻正（每条附形态特征）。

## 档案（鲲鹏 AArch64，GCC 12，基准 4.25.8，5 轮交替实测）

| # | 尝试 | 实测 | 形态 | 教训 |
|---|---|---|---|---|
| 1 | 37.x 三段批量 MapMergeFrom 整体移植 | merge 段 **-22%**（8674→10578ns） | heap（`je_*` 痕迹）、map 密集 | 批量分配只对 arena 成立；heap 形态退化为 n 次 malloc + 双倍遍历。详见 `shape-premise-checklist.md` |
| 2 | destruction 循环预取（ClearTable next-node + repeated Destroy +5 lookahead） | **-1.2~1.4%** | 浅树、指令/分配器主导 | 非 miss-bound 的 destroy 预取无收益。**先做 64MB cache 污染实验验证 miss-bound 假设**（实测污染仅让 destroy +1.5%，证伪） |
| 3 | 超前 reserve（读边界批量预留） | 零收益 | chunked-8KB 与 flat 输入混合 | 输入分布不保证 chunked 形态时，预留成本无处回收 |
| 4 | SVE 化 varint 编码变体 | 零收益 | 特征流小消息 | 指令层优化被调用频率/带宽吸收；且 varint 编码不在 parse 热路径主段 |
| 5 | UTF-8 校验 ASCII 快路径的生产外推 | 微基 **+4%**，生产 **≈0** | 该函数生产占比 0.01% | **外推铁律标准反例**：微基 % × 生产占比 ≠ 生产收益。优化前先查目标函数的生产火焰图占比 |
| 6 | mapmerge 表预扩容（reserve）的先验估计 | 先验 25~35%，实测 **+0.46%** | 短 key | resize 链 rehash 实测 ~0.2µs/merge；先验高估 50 倍——**<5% 预估增量的优化必须实测后再投入** |
| 7 | 强类型 clear / 桶上界维护 | 放弃（成本效益不过） | — | 指令级证据：ClearTable 507 insns 中 ~25 个结构周期是扫描/遍历/类型分发骨干，<3~5% 上限 vs 核心数据结构风险 |

## 指令级证据示例（负结果为什么是负）

以 #2 为例：ClearTable 占 destroy 段 51%（1272/9191 样本，~22ns/entry ≈ 45 cycles；507 insns：74 ldr、86 branches、32 个内联 delete 位点、3 个间接调用）。SPE 显示该段 l1d-miss ≈0、llc-miss = 0——**非 miss-bound 实锤**，预取（为 miss 设计的机制）必然无收益。这就是"先做污染实验/查 miss 计数再动手"的完整证据链形态。

## 使用纪律

1. **动手前先查本档案**——同形态下已证伪的方向直接跳过
2. **新负结果入档时必须附形态特征**（分配器/消息尺寸/树深度/占比）——它是"换形态可能翻正"判断的唯一依据
3. **正结果的边界同样记录**：mapmerge-copyctor 在 heap 形态 +5.6%，在 arena 形态未验证（37.x 批量方案可能反而更优）
