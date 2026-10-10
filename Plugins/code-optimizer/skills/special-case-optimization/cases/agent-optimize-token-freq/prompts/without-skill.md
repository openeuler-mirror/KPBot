请优化词频热点统计函数 hot_token_count，使其在保证正确的前提下尽可能快。

要求：
1. 待优化源码：src/kernel.rs（函数 hot_token_count）。
2. 保持函数签名 `pub fn hot_token_count(tokens: &[u32], vocab_size: usize) -> u64` 不变。
3. 将优化后的完整实现写入 opt/kernel_opt.rs（单个 .rs 文件，含 pub fn，不要引用 src/ 里的模块）。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 前置条件：所有 token < vocab_size；结果必须与参照精确一致（整数计数，不允许误差）。
6. 完成后用一段话说明你的快路径 guard 条件与 fallback 保持策略。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行 shell 命令。
