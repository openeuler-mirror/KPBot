# DiskANN 平台检查表

先声明任务范围：跨平台源码调查、Rust/C++ 基准执行、SSD 查询或鲲鹏实机验收。按范围检查，工具缺失不能自动扩大为全任务硬失败。

| 范围 | 证据与适用条件 |
| --- | --- |
| 源码 | 当前 commit 与 dirty diff；发布源码则版本、内容摘要、补丁清单 |
| 构建 | Rust 查看 Cargo.toml、rust-toolchain.toml、features；C++ 查看实际 CMake 和编译器 |
| CPU/ISA | CPU 型号、socket/core/SMT、运行亲和性；编译目标和实际反汇编共同证明指令路径 |
| NUMA | 有 NUMA 时记录拓扑、CPU set 与 memory node；没有时明确 not_applicable |
| SSD | 仅 SSD 测试要求有效存储、索引文件、后端支持和必要的内核能力 |
| 物理布局 | 布局实验记录 extent、文件系统、设备与拷贝/整理对照 |
| 鲲鹏验收 | 需要声明的 Linux/AArch64 目标和实际硬件，其他平台的结果不能替代 |

perf、iostat、filefrag 等仅在相应诊断依赖时要求；权限不足保存错误与缺口。来源为当前检出的构建文件、CLI 和 collect_env.py 本次输出，不预置机器型号、线程号或向量长度。
