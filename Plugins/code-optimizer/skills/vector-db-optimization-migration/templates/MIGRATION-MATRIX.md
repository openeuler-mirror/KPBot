# 迁移追溯矩阵

> 每机制一行；在 `notes` 或表头标注 engine 与 adapter；不得留空行或用“应该/大概”替代证据。
> 源码位置写 `relative-path::symbol`；行号只作辅助并绑定 commit。
> 证据标记：`[source]` / `[experiment]` / `[inference]` / `[unverified]`。

| mechanism_id | type | source@commit | source 位置 | target@commit | target 位置 | gate | disposition | 等价证据 | 性能证据 | verdict | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M001 | macro_optimization | `<sha>` | `path::symbol` | `<sha>` | `path::symbol` | G0/G2/G3 | KEEP | G0 bit-exact | ABBA + noise | KEEP | <notes> |
| M002 | structural_enabler | `<sha>` | `path::symbol` | `<sha>` | `path::symbol` | G0/G1 | ADAPT | UT + topk | N/A (enabler) | ADAPT | <notes> |
| M003 | kernel_library | `<sha>` | `<lib>::symbol` | `<sha>` | `path::symbol` | G2/G3 | REPLACE | bit-exact vs upstream | ABBA | KEEP | <notes> |
| M004 | macro_optimization | `<sha>` | `path::symbol` | N/A | N/A | G0 | DROP | 无源码作用点 | N/A | DROP | <notes> |

## 填写规则

- `type`：`kernel_library / non_macro_source / structural_enabler / macro_optimization / build_only / dead_or_absorbed / test_or_harness`。
- `disposition`：`KEEP / ADAPT / SPLIT / OPT_IN / REPLACE / DROP / DEFER`。
- `verdict`：只允许上述处置 + 门禁状态；不得写“应该有效”。
- `DROP`/`DEFER` 必须在 `notes` 写理由与新证据触发条件。
- `性能证据` 必须写数据集、环境、轮次、同窗条件；没有就填 `N/A` 或 `[unverified]`。
- 一个机制一个 patch；组合收益另开实验并在 notes 标注。

## 覆盖检查

- [ ] 每个源资产都有对应行（含排除项）。
- [ ] 每个 `KEEP`/`ADAPT` 都有目标版本作用点。
- [ ] 每个 `REPLACE` 都写明上游等价实现与差异。
- [ ] 每个 `OPT_IN`/`DEFER` 都写明触发条件。
- [ ] 所有 `DROP` 都有理由，不是“没时间做”。

