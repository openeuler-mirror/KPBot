# 任务：Java(JNI) 接入的 RocksDB 写停顿诊断

请先调用 `rocksdb-perf-diagnostics` skill（用 skill 工具加载），再按它的方法论完成下面的任务。

我们的服务通过 Java(JNI) 使用 RocksDB，不是 C++ 直连。最近写 P99 偏高、偶发写停顿。请给出：

1. 可以用哪些 RocksDB 观测手段；
2. 每个手段在 Java 侧对应的 API；
3. 哪些 C++ 手段在 Java 不可用、应如何替代；
4. 下一步采集计划。

要求：

- 用中文输出，给出依据。
- 不要对数据库做任何破坏性操作。
- 不要臆造 Java 中不存在的 API。
- 将最终回答写入 `/workspace/results/answer.md`。
