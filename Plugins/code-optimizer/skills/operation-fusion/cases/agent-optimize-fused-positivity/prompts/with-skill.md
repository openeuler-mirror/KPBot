请使用 operation-fusion skill 优化函数 weighted_positivity_score。

要求：
1. 待优化源码：src/kernel.rs（函数 weighted_positivity_score）。
2. 保持函数签名 `pub fn weighted_positivity_score(a: &[f64], b: &[f64]) -> f64` 不变。
3. 将优化后的完整实现写入 opt/kernel_opt.rs（单个 .rs 文件，含 pub fn，不要引用 src/ 里的模块）。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 语义不变：v = a[i]*(b[i]-0.5)+0.25，仅累加 v > 0 的项；相对误差 < 1e-9。
6. 完成后用一段话说明你做了哪些优化。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行 shell 命令。
