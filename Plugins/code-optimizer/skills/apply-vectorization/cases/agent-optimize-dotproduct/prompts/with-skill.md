请使用 apply-vectorization skill 优化点积函数 dot_product。

要求：
1. 待优化源码：src/kernel.c（函数 dot_product，接口见 src/kernel.h）。
2. 保持 src/kernel.h 中声明的函数签名不变。
3. 将优化后的完整实现写入 opt/kernel_opt.c，头文件用 #include "kernel.h"。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 优先使用 ARM NEON/SVE 向量化 + reduction 改写，保证数值正确（相对误差 < 1e-4）。
6. 完成后用一段话说明你做了哪些优化。