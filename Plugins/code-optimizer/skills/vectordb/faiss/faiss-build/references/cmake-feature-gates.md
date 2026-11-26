# CMake 特性开关

## 最小示例

```cmake
option(FAISS_ALGORITHM_CANDIDATE "Enable the candidate implementation" OFF)
if(FAISS_ALGORITHM_CANDIDATE)
  target_compile_definitions(faiss PRIVATE FAISS_ALGORITHM_CANDIDATE)
endif()
```
```cpp
#if defined(FAISS_ALGORITHM_CANDIDATE)
// 受保护的候选路径，包含必要的平台/形状guard。
#else
// 原始实现。
#endif
```
按实际目标组织开关；若工程生成多个库变体，分别核对所用 target。宏名应描述机制和范围，不承载个人轮次或累积优化版本。

## 状态与归因

开关默认关闭，候选保留 fallback。每次只启用一个主要机制；已有基线宏集按本次 manifest 冻结。宏状态与适用条件记录在实验产物中，不由名字或源文件是否出现判断 accepted/rejected。
候选构建可与基线的宏不同，公共 flags、无关特性和运行参数保持一致。目标允许的质量/精度变化另行声明实验。

## 启用证据

检查 CMakeCache、实际目标的 compile_commands 与详细构建命令；必要时反汇编并采样确认路径。strings 只能提供线索。未进入编译的宏、未命中的 guard 和未执行的内核分别说明，不能据此声称性能改善。
