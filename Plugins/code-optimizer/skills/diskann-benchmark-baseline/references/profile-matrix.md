# DiskANN Profiling 矩阵

| 问题 | 最小证据 | 进一步采集 |
| --- | --- | --- |
| CPU 还是 I/O 主导？ | wall time、QPS、perf cycles/instructions、task-clock、iostat await/util、应用 I/O wait 计时 | off-CPU profiling、syscall trace |
| 哪个 CPU 函数？ | `perf record -g`、可解析符号 | 反汇编加源码映射 |
| 实际生成 SIMD？ | 二进制身份、`objdump -d`、采样指令地址 | 可用时增加硬件指令计数 |
| Cache/内存问题？ | cache/branch/TLB counter、IPC、working set | NUMA counter、内存带宽工具 |
| io_uring 是否重叠？ | submit/wait/drain 计时、SQE/CQE 数、完成事件时间 | 允许时使用 tracepoint/BPF |
| 布局是否有效？ | I/O 数/字节、LBA 或 sector 序列、extent、相同逻辑查询工作 | block trace、受控 copy/reorder 实验 |

Profiling 数据旁必须同时保存 benchmark 正确性和工作量计数。
