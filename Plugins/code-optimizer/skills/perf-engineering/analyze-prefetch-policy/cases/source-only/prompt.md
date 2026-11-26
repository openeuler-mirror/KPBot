# 独立源码分析提示

此提示供人工或独立 agent 复核使用，不由 tier A 的 `run.py` 执行，也不是 with/without A/B 用例。开展独立复核时，只向分析者提供 skill、下列请求和两份 fixture；报告生成后，再由审查者按 `review-checklist.md` 核对。

## 请求

请使用 analyze-prefetch-policy 分析本目录 fixture.rs 和 fixture.c 中的预取策略，分别说明 x86_64 与 aarch64 的情况。关注 harvest_probe、read_one 和 write_one：预取什么数据、目标缓存层级、读写类型、触发条件和相对实际访问的提前距离。

当前仅有这两个源文件，没有编译产物、构建参数、CPU 型号或运行记录。请根据现有材料交付 `PREFETCH-POLICY.md` 和符合 prefetch-policy/v1 契约的 `prefetch-policy.json`，写到本轮独立输出目录，并说明结论的证据范围及还缺什么。保留可核对的文件路径、行号和符号；不编译、不执行样例程序。
