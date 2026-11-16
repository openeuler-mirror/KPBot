# 源码证据协议

## 追踪模板

1. 入口：benchmark/CLI/API 函数。
2. 配置：参数解析和默认值。
3. 所有权：持有 index、reader、cache、scratch、queue、LUT 或 buffer 的对象。
4. 控制流：loop、branch、frontier 选择、cache hit/miss、submit/wait/drain。
5. 数据流：source buffer → offset → destination slot → consumer。
6. 序列化：header、node record、alignment、sector 计算。
7. 运行边界：syscall、thread pool、allocator、SIMD intrinsic、device I/O。

## 证据规则

- 引用当前 commit 的 `relative/path:line-line`。
- 同时记录 symbol 名，便于行号漂移后重新定位。
- 注释与可执行代码冲突时，注释不能作为充分证据。
- generated file 和 benchmark wrapper 只有实际进入本次构建时，才能作为行为证据。
- 判断时间占比、cache miss、阻塞时长或实际指令频率时，需要 `perf`、`strace`、`bpftrace` 或 counter。

## 输出标签

- **事实**：可从源码或测量输出直接观察。
- **推断**：通过逻辑得出，并说明依赖的事实。
- **未知**：证据缺失或可能选择多种实现。
