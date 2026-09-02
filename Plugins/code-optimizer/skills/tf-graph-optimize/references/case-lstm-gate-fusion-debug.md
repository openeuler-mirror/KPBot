# LSTM gate 融合 Bug 排查案例（脱敏）

> 脱敏自调试记录。展示「局部优化不生效」的根因定位方法论。

## 一、现象

A 优化点（Tensordot 折叠）生效，但 E 优化点（`Sigmoid→Mul`/`Tanh→Mul` 融合）完全不生效：

| op | 优化前 | 部署后 |
|---|---|---|
| `_FusedMatMul` | 209 | 374（A 生效） |
| `MatMul` | 165 | 0（A 生效） |
| `_FusedSigmoidMul` | 0 | **0（E 未生效）** |
| `Sigmoid` | 341 | 341（无变化） |

## 二、排查过程（逐层排除）

1. **确认代码在二进制里**：`strings` 验证函数符号 + op 名在二进制 → 排除"没编译进去"。
2. **静态检查匹配条件**：fanout/输入数/control 依赖/device/T 全部满足 → 理论上应匹配。
3. **检查 remapper 循环结构**：发现 `if (IsMKLEnabled() && ...)` 大分支（约 200 行）包含 MKL 专属融合。

## 三、根因

`FindActivationAndMul` 的调用**被错误放在 `if (IsMKLEnabled() && ...)` 分支内**，而 A 优化点的调用在分支外。鲲鹏 aarch64 上 `IsMKLEnabled()` 返回 false，分支被跳过 → E 未执行、A 正常。

## 四、修复

把 E 的调用从 IsMKLEnabled 分支移到分支外（无条件执行）。提交：`842ee6ca`。

## 五、验证结果

| op | 修复前 | 修复后 |
|---|---|---|
| `_FusedSigmoidMul` | 0 | 121 |
| `_FusedTanhMul` | 0 | 176 |
| `Sigmoid` | 341 | 44 |
| `Tanh` | 176 | 0 |

op 累计耗时 +6.2%（其中 A 贡献 +1.6%、E 贡献 +4.7%）。

## 六、经验教训（可复用方法论）

1. **新增 remapper 规则注意调用位置**是否在 `IsMKLEnabled()`/`DNNL_AARCH64_USE_ACL` 等条件分支内；纯 Eigen 实现应放分支外。
2. **「A 生效、E 不生效」局部失效优先怀疑规则放置位置/条件分支**，而非匹配逻辑本身（静态条件全满足仍不生效，往往是"根本没执行到"）。
3. **用 `strings` 检查二进制符号**能快速区分"没编译进去" vs "运行时没触发"。
