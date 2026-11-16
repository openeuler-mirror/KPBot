# PQ 查表内核

对于 C 个 chunk、每 chunk K 个 centroid，单查询 LUT 包含 `C × K` 个距离。K=256 且 entry 为 f32 时，LUT payload 为 `C × 256 × 4 B`。

8-bit PQ code 每 chunk 从 256 个 entry 中选择一个。4-bit code 每 chunk 从 16 个 entry 中选择一个，并通常每字节打包两个 code；实际布局不同时以源码为准。

优化前回答：

- code 是否连续并对齐？
- 查表是 scalar gather bound、arithmetic bound 还是 reduction bound？
- 每次调用评估多少向量？
- batch 是否允许不同候选复用 LUT？
- table-lookup/shuffle 策略是否适配当前 LUT 布局？
- tail 和奇数 chunk 是否常见？

multi-accumulator、循环重排、手写汇编和预取都是待测机制；依据当前反汇编、热点和端到端对照判断，不预设收益方向。
