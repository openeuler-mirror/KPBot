请使用 precision-transform skill 优化量化点积函数 quant_dot。

要求：
1. 待优化源码：src/kernel.rs（函数 quant_dot）。
2. 保持函数签名 `pub fn quant_dot(q: &[i8], w: &[i8]) -> f64` 不变。
3. 将优化后的完整实现写入 opt/kernel_opt.rs（单个 .rs 文件，含 pub fn，不要引用 src/ 里的模块）。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 精度契约：返回值与逐元素 f64 累加的参照相比相对误差 < 1e-3；注意给出整数累加的溢出边界论证。
6. 完成后用一段话说明你做了哪些优化（含类型变换依据）。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行 shell 命令。
