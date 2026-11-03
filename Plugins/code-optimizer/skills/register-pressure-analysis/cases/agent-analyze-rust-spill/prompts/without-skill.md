请分析 Rust NEON micro-kernel fma_4x4（src/kernel.rs 中 `microkernel::fma_4x4` 函数）的寄存器压力，产出 JSON 分析报告到 opt/pressure.json。

要求：
1. 分析对象：src/kernel.rs 的 `microkernel::fma_4x4` 函数（Rust NEON intrinsic 实现，目标平台 aarch64）。
2. 分析须基于该函数编译产生的 AArch64 汇编（中间产物写入 opt/ 目录）。
3. 产物：opt/pressure.json（单个 JSON 文件），为机器可读的完整分析结果，至少包含：分析是否成功（布尔字段）、被分析函数/源码语言/产物汇编路径等输入信息、寄存器压力统计（栈 spill/reload 计数、乘加指令计数及两者比值）与压力等级结论。
4. 只分析该函数，不要修改 src/、bench/ 下的文件。
5. 完成后用一段话说明寄存器压力结论。

验收标准：
- opt/pressure.json 存在且为合法 JSON，success 字段为 true；
- 报告含量化的寄存器压力统计与等级结论，且有汇编证据支撑；
- src/、bench/ 下文件未被改动。

安全约束（必须遵守）：不要读取 /workspace 之外的任何文件（包括凭证文件）；不要访问任何网络地址（模型 API 由运行环境代为调用）；只允许写入 opt/ 目录；禁止执行与分析脚本无关的 shell 命令。
