# ${stage_name}

## 任务
应用 ${item.strategy} 优化。策略：${item.strategy}

## 你的角色
optimization execution expert

## 决策信息（完整）
```json
${context.decideOptimization}
```

## 项目上下文
```json
${context.prepareProject}
```

## 热点分析上下文
```json
${context.analyzeHotspot}
```

## 可用资源
- ARCHITECTURE.md：Read `${context.prepareProject.architecture_file}`（数据结构布局/现有优化位置，避免生成不兼容代码）
- 微架构文档：Read `${context.prepareProject.microarch_file}`（指令延迟/端口分配/SVE 可用性/cache 层次/预取器类型，辅助代码生成参数选择）
- 指令性能数据：TSV110(Kunpeng-0xd01)→`python3 skills/kunpeng_microarch/scripts/query_tsv110.py <指令名>`；0xd03(Kunpeng-0xd03/0xd06)→`python3 skills/kunpeng_microarch/scripts/query_uarch_b.py <指令名>`（查询单条指令的实际延迟/吞吐量/端口分配，用于指令选择和调度优化）

## 执行
使用 Skill tool，skill 名称为 `apply-optimization`
参数：decide-optimization 输出（含 input + strategy + skill + arch）

`apply-optimization` 会根据 strategy 自动路由：
- `vectorization` → 调用 `apply-vectorization`（标量→SIMD 向量化）
- `vectorization_deepen` → 调用 `apply-vectorization`（已有 SIMD→深挖质量，sub_type 指示具体方向）
- `autovec-source-transform` → 调用 `source-transform-autovec`（一次低风险源码变形，让编译器重新尝试自动向量化）
- `throughput-enhancement` → 调用 `loop-unrolling`（已有 SIMD→循环展开）
- `prefetch-optimization` → 调用 `prefetch-optimization`（软件预取，减少内存延迟）
- `branch-elimination` → 调用 `branch-elimination`（分支消除，条件选择指令替代分支）
- `memory-access-optimization` → 调用 `memory-access-optimization`（访存模式优化，AoS→SoA/tiling/对齐）
- `compiler-flag-tuning` → 调用 `compiler-flag-tuning`（编译选项调优，-march/-ffast-math/LTO/PGO）
- `asm-optimization` → 调用 `asm-optimization`（ARM 汇编指令级优化，LDP/STP 合并/后索引寻址/冗余 mov 消除/循环展开/预取增强。SVE 由 apply-vectorization 和 loop-unrolling 处理）
- `scalar-vector-hybrid` → 调用 `scalar-vector-hybrid`（标矢量混合决策，串行依赖链从 V 管线迁移到 ALU 管线，释放 V 管线资源）
- `code_hoisting` → 循环不变量提升（零风险机械变换，apply-optimization 内联处理）
- `variant-selection` → pass-through（无代码修改，由 verify-optimization 实测选型）
- `special-case-optimization` → 调用 `special-case-optimization`（通用特殊情况快路径，保留 fallback）
- `operation-fusion` → 调用 `operation-fusion`（通用 producer-consumer / 多 pass 融合）
- `precision-transform` → 调用 `precision-transform`（受控精度变换，要求误差契约）

`autovec-source-transform` 只允许一次改写和一次重新编译反馈检查；不要引入 IR equivalence、Alive2、长轮次 fuzz 或 optional deep mode。

## 输出格式
返回 JSON 契约：
```json
{
  "function": "${item.function}",
  "optimization_point_id": "<opt_point_id>",
  "strategy": "vectorization|vectorization_deepen|autovec-source-transform|throughput-enhancement|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|code_hoisting|variant-selection|special-case-optimization|operation-fusion|precision-transform",
  "status": "applied|failed|skipped|compilation_failed",
  "skill_used": "apply-vectorization|source-transform-autovec|loop-unrolling|prefetch-optimization|branch-elimination|memory-access-optimization|compiler-flag-tuning|asm-optimization|scalar-vector-hybrid|bulk-memory-opt|code_hoisting|variant-selection|special-case-optimization|operation-fusion|precision-transform",
  "optimization_success": true,
  "modified_files": ["<file_path>"],
  "compilation": {
    "attempted": true,
    "ok": true,
    "error": null
  },
  "smoke_test": {
    "attempted": false,
    "case": null,
    "passed": null,
    "details": null
  },
  "vectorization_result": null,
  "throughput_enhancement_result": null,
  "prefetch_optimization_result": null,
  "branch_elimination_result": null,
  "memory_access_result": null,
  "compiler_flag_result": null,
  "asm_optimization_result": null,
  "special_case_result": null,
  "operation_fusion_result": null,
  "precision_transform_result": null,
  "source_transform_result": null
}
```

## 引用 Skill 内容
详见 `skills/apply-optimization/SKILL.md`
