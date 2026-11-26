请使用 memory-access-optimization skill 优化矩阵乘法函数 matmul。

要求：
1. 待优化源码：src/kernel.c（函数 matmul，接口见 src/kernel.h）。
2. 保持 src/kernel.h 中声明的函数签名不变。
3. 将优化后的完整实现写入 opt/kernel_opt.c，头文件用 #include "kernel.h"。
4. 只优化该函数，不要修改 src/、bench/ 下的其它文件。
5. 重点优化访存模式：循环重排（如 ikj 顺序）、缓存分块（tiling）等，提升缓存利用率。
6. 保证数值正确（与参照的相对误差 < 1e-3）。
7. 完成后用一段话说明你做了哪些优化。