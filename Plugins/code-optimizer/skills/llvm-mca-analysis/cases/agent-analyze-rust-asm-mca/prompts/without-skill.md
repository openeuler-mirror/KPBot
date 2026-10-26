请分析 src/kernel.rs 中 calc::dot 函数在 ARM 上的流水线性能瓶颈，产出 JSON 报告到 opt/analysis.json。

要求：
1. 分析对象：src/kernel.rs 的 `calc::dot` 函数（Rust 源码，`--function` 传 `calc::dot`）。
2. 用技能自带脚本做分析，走 Rust 源码输入路径（`--source src/kernel.rs --function calc::dot --source-language rust`，微架构用 `--uarch hip08`）。
3. 把脚本的完整 JSON 输出保存到 opt/analysis.json（单个文件，不要改写内容）。
4. 只分析该函数，不要修改 src/、bench/ 下的文件。
5. 完成后用一段话说明瓶颈归因结论。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行与分析脚本无关的 shell 命令。
