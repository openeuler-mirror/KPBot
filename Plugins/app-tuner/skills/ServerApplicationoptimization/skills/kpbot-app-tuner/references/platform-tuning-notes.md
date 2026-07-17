# 平台专项调优说明 / Platform Tuning Notes

## NUMA

多插槽服务器必须检查：

- NUMA 拓扑
- CPU 与内存节点分布
- 线程和中断是否跨节点漂移
- 远端内存访问代价

## THP

建议显式检查 THP 状态。

数据库场景下，`THP=always` 往往是需要重点确认的风险项，应结合业务特点判断是否需要关闭或改为 `madvise`。

## HugePages

HugePages 更适合以下场景：

- 内存带宽或 TLB 压力明显
- 大页映射能够显著减少页表开销

纯 CPU-bound 场景下，HugePages 不一定带来明显收益，应避免默认假设其有效。

## ARM / aarch64

ARM / aarch64 平台需额外关注：

- 编译目标架构参数
- 不同平台的计数器和采样质量差异
- perf 证据边界
- 构建系统是否正确打开目标平台优化

若目标平台为 Kunpeng 或其他 ARM64 服务器，应在编译器优化与性能结论中显式注明平台背景。
