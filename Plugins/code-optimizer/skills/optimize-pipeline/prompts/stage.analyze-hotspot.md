# ${stage_name}

## 任务
对单个函数进行动态微架构分析和静态代码扫描，发现多个优化机会，输出优化点列表。

## 你的角色
performance analysis expert

## 上下文
聚焦单函数上下文（仅当前子任务的函数和文件）：
```json
{
  "repo": ${context.prepareProject.repo},
  "target": {
    "source_files": ["${context.current_sub_task.source_file}"],
    "entry_functions": ["${context.current_sub_task.function}"]
  },
  "baseline": ${context.prepareProject.baseline},
  "sub_task": {
    "id": ${context.current_sub_task.id},
    "function": "${context.current_sub_task.function}",
    "source_file": "${context.current_sub_task.source_file}",
    "lines": ${context.current_sub_task.lines},
    "priority": "${context.current_sub_task.priority}",
    "cross_case_weight": ${context.current_sub_task.cross_case_weight},
    "cpu_percent": ${context.current_sub_task.cpu_percent},
    "coverage": ${context.current_sub_task.coverage},
    "case_distribution": ${context.current_sub_task.case_distribution}
  },
  "test_method": "${context.test_method}",
  "intent": ${context.parseIntent},
  "architecture_file": "${context.prepareProject.architecture_file}",
  "performance_profile": ${context.testcaseAnalysis.performance_profile},
  "microarch_file": "${context.prepareProject.microarch_file}"
}
```

## 执行
使用 Skill tool，skill 名称为 `analyze-hotspot`
参数：上述聚焦单函数上下文

动态分析三件套：perf stat（微架构事件：IPC/branch miss rate/cache miss rate）→ perf annotate（热点指令定位）→ perf spe（cache/branch miss 精确归因）。

对 C/C++ 目标函数额外采集一次 compiler vectorization feedback，作为 `autovec-source-transform` 的唯一触发依据。Clang/BiSheng 使用 `-Rpass=loop-vectorize -Rpass-missed=loop-vectorize -Rpass-analysis=loop-vectorize`；GCC 使用 `-fopt-info-vec-optimized -fopt-info-vec-missed`。该步骤只读编译诊断，不修改源码或构建配置；不可用时记录 fallback reason。

如果静态扫描或优化点判断涉及 NEON/SVE/SVE2 intrinsic、inline asm 或汇编指令事实，必须先调用 repo 内统一入口并记录 JSON evidence：

```bash
cd <pipeline_root>/skills/arm-instructions-query
python3 scripts/arm_query.py instruction --name <mnemonic> --family <neon|sve|sve2> --json
python3 scripts/arm_query.py intrinsic --name <intrinsic> --family <neon|sve|sve2> --json
```

若 `<pipeline_root>` 未知，从已加载的 `skills/analyze-hotspot/SKILL.md` 真实路径向上两级解析；当 skill 位于 `~/.claude/skills/...` 软链接下时，使用 resolved path，不要使用过期拷贝。

不要调用 `query.py ... --json`。`query.py` 只允许作为人类可读 fallback。若本阶段输出了“已有 NEON/SVE 指令正确”“目标支持/不支持某指令”“没有更好 intrinsic/指令”等判断，`static_analysis.instruction_query_evidence` 必须非空。

## 输出格式
返回 JSON 契约：
```json
{
  "function": "<function_name>",
  "source_file": "<source_file_path>",
  "lines": [start_line, end_line],
  "dynamic_analysis": {
    "status": "ok|partial|unavailable",
    "perf_stat": {
      "ipc": 0.72,
      "branch_mispredict_rate_pct": 12.3,
      "l1d_cache_miss_rate_pct": 8.5,
      "llc_cache_miss_rate_pct": 2.1,
      "cpu_clock_ms": 450
    },
    "perf_annotate_used": true,
    "perf_annotate_top5": [
      { "instruction": "...", "cpu_pct": 28.3, "source_line": 45 }
    ],
    "perf_spe_used": true,
    "perf_spe_samples": {
      "top_cache_miss_instructions": [],
      "top_branch_miss_instructions": []
    }
  },
  "static_analysis": {
    "nested_loops": 2,
    "has_simd": false,
    "simd_type": null,
    "current_parallelism": null,
    "data_dependencies": "none",
    "memory_access_pattern": "stream",
    "stride_bytes": 4,
    "generic_shape": {
      "input_scale": "empty|unit|small_fixed|power_of_two|general|unknown",
      "parameter_constancy": [],
      "repeated_passes": 0,
      "intermediate_lifetime": "local_only|escaped|unknown",
      "precision_profile": { "input_type": "fp32|fp64|fp16|bf16|int8|unknown", "accumulation_type": "same|promoted|unknown", "tolerance_available": false },
      "boundary_branch_ratio": null
    },
    "estimated_working_set_kb": 512,
    "branch_pattern": "unpredictable_in_loop",
    "compiler_flags": { "optimization_level": "-O2", "march_specified": false, "ffast_math": false },
    "compiler_vectorization_feedback": {
      "available": true,
      "compiler": "clang|gcc|bisheng|unknown",
      "command": "<single-TU compile command with vectorization remarks>",
      "optimized_loops": [],
      "missed_loops": [{ "line": 42, "reason": "<missed-vectorization reason>", "suggested_transform": "<low-risk transform or null>" }],
      "fallback_reason": null
    },
    "instruction_query_evidence": [
      {
        "query_type": "isa_instruction|acle_intrinsic",
        "family": "neon|sve|sve2",
        "query": "<mnemonic-or-intrinsic>",
        "tool": "arm_query.py",
        "command": "python3 scripts/arm_query.py instruction --name <mnemonic> --family <family> --json",
        "decision": "used|filtered|not_found|query_failed",
        "evidence": { "syntax_checked": true, "features": [], "pseudocode_checked": true }
      }
    ]
  },
  "optimization_points": [
    {
      "id": "func_opt1",
      "type": "vectorization|vectorization_deepen|autovec-source-transform|throughput-enhancement|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|math-rewrite|algorithm-substitution|variant-selection|code_hoisting|special-case-optimization|operation-fusion|precision-transform",
      "sub_type": "lane_width_partial|remainder_scalar|load_pair_missing|register_underutilized|accumulator_serial|interleave_missing|loop_invariant_hoist|mixed_loop_split|reduction_canonicalize|temporary_load_store|branch_simplify|boundary_peel|layout_fast_path|local_layout_normalization|producer_consumer_fusion|bulk_memory_idiom|const_mode_fast_path|empty_input|unit_length|small_fixed_size|power_of_two|constant_parameter|zero_identity|all_zero_sparse|alignment_fast_path|remainder_kernel|mode_flag|optional_output_alias|in_place_fast_path|broadcast_scalar|numeric_domain|null",
      "target_arch": "neon",
      "confidence": 0.9,
      "expected_speedup": "2-4x",
      "priority": 1,
      "evidence": {
        "static": "双层嵌套循环 + 标量运算 + 连续访存 + 无跨迭代依赖",
        "dynamic": "IPC 仅 0.72 → CPU 停顿严重"
      }
    }
  ],
  "skipped_points": [
    { "type": "branch-elimination", "reason": "循环体内无条件分支" }
  ],
  "status": "analyzed" | "empty"
}
```

## 引用 Skill 内容
详见 `skills/analyze-hotspot/SKILL.md`
