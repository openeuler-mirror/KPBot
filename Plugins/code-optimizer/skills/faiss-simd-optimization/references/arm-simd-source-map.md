# ARM SIMD 源码导航

常见入口包括 `faiss/utils/distances*`、`simdlib*`、`faiss/impl/code_distance/*` 及算法专属 quantizer/scanner。不同版本可能使用不同目录和 dispatch 组织。
先找到调用方和实际类型，再追踪架构宏、模板或运行时分派；查看相关翻译单元的 march、向量长度、自动向量化及 intrinsic。可选的后端或单独架构源文件必须确认存在、被编译且被调用。
记录标量/generic、NEON、SVE/SVE2 或仿真路径的适用条件。编译器支持某个 ISA 不代表目标符号使用了它；全局开启架构宏也可能暴露未实现的实例化/链接路径，应在修改前核对覆盖范围。
