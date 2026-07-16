---
name: source-transform-autovec
description: 对编译器 missed vectorization 的热点循环执行一次低风险源码变形，让编译器重新尝试自动向量化。适用于 apply-optimization 调用。
---

# Autovec 源码变形

你是一位轻量源码变形专家。你的任务是在编译器已经报告热点循环未能自动向量化时，做一次局部、低风险、可回退的源码改写，使编译器更容易识别并向量化主循环。

本 skill 不生成 NEON/SVE/SME intrinsics，不写汇编，不做 IR equivalence、Alive2、长轮次 fuzz，也没有 optional deep mode。正确性验证交给现有编译、测试、hidden checks、性能对比和反汇编检查。

## 输入

```json
{
  "function": "<function_name>",
  "source_file": "<file_path>",
  "lines": [start, end],
  "sub_type": "loop_invariant_hoist|mixed_loop_split|reduction_canonicalize|temporary_load_store|branch_simplify|boundary_peel|layout_fast_path|local_layout_normalization|producer_consumer_fusion|bulk_memory_idiom|const_mode_fast_path",
  "strategy_payload": {
    "compiler_feedback_before": {},
    "missed_reason": "<compiler missed-vectorization reason>",
    "guard_condition": "<optional cheap predicate>",
    "fallback_required": true
  },
  "context": {
    "prepareProject": {},
    "analyzeHotspot": {}
  }
}
```

## 执行步骤

1. Read `source_file` 的目标函数，确认 `compiler_feedback_before` 中存在目标循环的 missed-vectorization 记录；若没有明确 missed reason，返回 `success=false`。
2. 只选择一个 `sub_type` 执行一次局部改写。若需要多轮修补、跨文件证明、公开 API 变化、全局数据布局迁移或复杂别名证明，直接拒绝。
3. 允许的低风险变形：
   - 循环不变量提升。
   - 简单 mixed loop 拆分。
   - 简单 reduction 规范化，但不得改变浮点结合律或整型溢出语义。
   - 重复 load/store 的临时变量整理。
   - 无副作用的简单分支规整。
   - 边界/尾部逻辑剥离，让主循环更干净。
   - 连续布局 fast path，例如 `stride == 1`、contiguous、aligned 路径。
   - 局部 layout normalization，例如小通道交错访问转局部连续访问；不得改变公开数据布局。
   - 局部 producer-consumer 融合，减少中间 buffer 或重复遍历。
   - 简单循环改 `memcpy` / `memset` 等 bulk memory idiom。
   - 常量 mode / flag / 参数 fast path。
4. 保留原语义路径。需要 guard 的 fast path 必须使用已有参数或 cheap predicate；不能引入比原循环更贵的全量扫描。
5. 输出 `optimized_code` 和 `source_transform_result`，由 `apply-optimization` 负责写入源码、重新编译并采集一次 compiler feedback。

## 输出

```json
{
  "source_transform_result": {
    "success": true,
    "sub_type": "<sub_type>",
    "compiler_feedback_before": {},
    "compiler_feedback_after": null,
    "vectorized_by_compiler": null,
    "guard_condition": "<condition or null>",
    "fallback_preserved": true,
    "original_code": "<原始片段>",
    "optimized_code": "<改写后片段>",
    "modified_region": "function|loop|branch",
    "validation_focus": ["public correctness", "hidden correctness", "compiler feedback or disassembly", "no benchmark regression"],
    "skipped_reason": null,
    "error_message": ""
  }
}
```

## 规则

- 不做源码写入；只返回可由 `apply-optimization` 应用的文本。
- 一次调用只做一次变形；失败后不迭代修补。
- 不添加 `restrict`、不可靠 `assume_aligned`、`__builtin_assume` 或可能制造 UB 的提示。
- 高风险语义变形直接跳过，不通过额外深度验证扩大适用范围。
- 复杂 SIMD 代码生成继续走 `apply-vectorization`。
