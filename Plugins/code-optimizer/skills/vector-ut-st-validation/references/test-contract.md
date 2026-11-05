# 向量检索 UT/ST 契约

## 分层

| 层级 | 目标 | 示例 |
| --- | --- | --- |
| UT | 函数/模块契约、边界和错误路径 | 距离核尾部、buffer 对齐、短读、空候选 |
| Component | 模块组合但不跑完整数据集 | io_uring 提交/完成、feature dispatch、索引读取 |
| ST | 真实二进制、索引、线程、I/O 和部署组合 | build/load/search、并发查询、异常设备、回退 |
| Performance | 在功能通过后验证吞吐与正确性不变量 | 冻结矩阵、ABBA、geomean |

性能测试不能代替前三层。

## 风险到用例

- SIMD：标量参考、随机向量、NaN/Inf 策略、非向量宽度尾部、对齐/非对齐、不同 ISA dispatch。
- I/O：短读、部分完成、CQE 乱序、错误码、队列满、buffer 生命周期、取消与回退。
- 并发：线程数 1/N、共享只读数据、scratch 隔离、重复执行、竞争检测或等价证据。
- 布局：序列化/反序列化一致、边界节点、旧索引兼容、feature-off 原布局。
- 量化：标量或高精参考、编码/解码、候选/refine 一致、质量阈值不被暗改。

## 必测矩阵

1. baseline commit + feature-off；
2. candidate commit + feature-off；
3. candidate commit + feature-on；
4. 正常、边界和失败路径；
5. 单线程与目标并发；
6. 目标部署平台系统测试（注明硬件、ISA 与运行条件）；
7. 真实 benchmark 冒烟，但不把它当全部 ST；
8. 正式性能矩阵在 UT/ST 全部通过之后执行。

## `not_applicable`

并发、fallback、sanitizer 或兼容性确实不适用时必须记录：为何不适用、由谁确认、替代证据和剩余风险。空字符串或“无需测试”不合格。

## 合入门禁

- UT 和 ST 均有至少一个已执行用例且全部通过；
- feature-on/off、边界、并发、fallback 均为 pass 或有完整 N/A 理由；
- 报告绑定 commit、平台、构建参数和原始日志；
- 失败、跳过和 flaky 用例均被显式列出；
- UT/ST 报告为 `READY` 并完成所需核验后进入正式性能对照；最终采用依据完整质量与性能证据。
