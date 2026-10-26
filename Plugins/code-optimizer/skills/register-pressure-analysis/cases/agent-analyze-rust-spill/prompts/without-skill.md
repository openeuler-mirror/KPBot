请分析 Rust NEON micro-kernel fma_4x4 的寄存器压力，产出 JSON 报告到 opt/pressure.json。

要求：
1. 分析对象：src/kernel.rs 的 `microkernel::fma_4x4` 函数（Rust NEON intrinsic，`--function` 传 `microkernel::fma_4x4`）。
2. 先把 src/kernel.rs 编译为汇编：`rustc -O --target aarch64-unknown-linux-gnu -C target-feature=+neon --emit asm -o opt/kernel.s src/kernel.rs`（容器是 aarch64 原生，rustc 无需安装额外 target）。
3. 再调技能自带脚本做分析：`python3 /staging/skill/scripts/analyze_assembly_spill.py --asm opt/kernel.s --function microkernel::fma_4x4 --source-kind rust_intrinsics --language rust`（脚本路径用绝对路径 /staging/skill/scripts/——skill 装进 .opencode/skills/ 后脚本仍在 /staging/skill/scripts/ 原位可读），把脚本的完整 JSON 输出保存到 opt/pressure.json（单个文件，不要改写内容）。
4. 只分析该函数，不要修改 src/、bench/ 下的文件。
5. 完成后用一段话说明寄存器压力结论。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行与分析脚本无关的 shell 命令。
