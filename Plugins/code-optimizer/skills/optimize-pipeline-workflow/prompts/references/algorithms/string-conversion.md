# 字符串 / 数据转换 算法替代参考

供 `perspective-algorithm`（算法模式识别与替代研究员）检测时 Read 引用。聚焦**算法/数学层面**的改进方案，不含指令级优化。

---

## 1. 数值→字符串 / 字符串→数值

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| `sprintf` / `snprintf` | 查表整数→字符串 (如 fmtlib/fast_float) | O(N)，常数因子 ↓ 10-100× | C++ 标准实现 |
| `atoi/strtol` 逐字符 | SIMD 批量字符→数字解析 | O(N)，常数因子 ↓ 4-8× | NEON 批量减法 '0' + 范围检查 |
| 浮点→字符串 (Grisu/Dragon4) | Ryū / Schubfach 算法 | 常数因子 ↓ 5-10× | 纯整数运算，平台无关 |

**检测信号**：
- `sprintf(buf, "%d", val)` 或 `snprintf` 用于数值格式化
- `atoi/strtol/strtod` 调用在循环中逐字符串解析
- 浮点格式化：`log10` + `pow(10, exp)` 求小数位（Dragon4/Grisu 特征）

## 2. Base64 编解码

| 当前方案 | 替代方案 | 复杂度变化 | ARM 适配 |
|---------|---------|-----------|---------|
| 逐字节查表 (256 项) | 批量查表 (NEON TBL 16 字节) | 常数因子 ↓ 4-8× | NEON TBL 3 表 + 移位重排 |
| NEON TBL 批量 | SVE2 TBL (更大向量宽) | 常数因子 ↓ 进一步 2-4× | SVE2 平台 |

**检测信号**：
- `"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"` 查表字符串
- 3 字节→4 字符编码：`out[0] = table[in[0] >> 2]; out[1] = table[((in[0] & 3) << 4) | (in[1] >> 4)];`
