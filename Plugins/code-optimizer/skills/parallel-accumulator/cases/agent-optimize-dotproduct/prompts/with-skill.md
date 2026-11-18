请使用 parallel-accumulator skill 优化点积函数 dot。

要求：
1. 待优化源码：src/kernel.c（函数 dot，接口见 src/kernel.h）。
2. 保持 src/kernel.h 中声明的函数签名不变。
3. 将优化后的完整实现写入 opt/kernel_opt.c，头文件用 #include "kernel.h"。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 目标是消除单累加器的串行依赖链：用多个独立累加器展开（纯 C 多累加器，或 NEON 向量累加器 + 多累加器）。
6. 保证数值正确（浮点重排允许 ε 舍入差异，相对误差 < 1e-5）；尾处理不得回退成单累加器循环。
7. 完成后用一段话说明你做了哪些优化，并注明浮点结合律假设。

安全约束：
- 禁止读取凭证文件及任何不在 /workspace 下的文件。
- 只允许写入 opt/kernel_opt.c；禁止修改 src/、bench/ 下的任何文件。
- 禁止发起任何网络请求（模型 API 由运行环境代为调用）。