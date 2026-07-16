// =============================================================================
// optimization-round.js — Workflow 脚本
//
// 执行一轮优化的核心循环：
//   函数级 9 视角并行分析 → 合成 → 优化点级 Audit→Apply→Verify→Fix（串行）
//
// 由 optimize-pipeline Orchestrator 在每轮 profiling 后调用。
// Orchestrator 负责：用户交互、环境预检、DecomposeTasks、报告写入、轮次决策。
// 本 Workflow 负责：纯 agent 驱动的优化执行。
// 优化点按优先级排序后串行执行：每个点的 Apply 修改源码后 commit，
// 下一个点基于已提交状态操作，避免并行修改同一文件的冲突。
// =============================================================================

export const meta = {
  name: 'optimization-round',
  description: '执行一轮优化：9 视角并行分析 → 合成 → 优化点级 Audit→Apply→Verify→Fix（串行）',
  phases: [
    { title: 'Analyze', detail: '9 视角并行分析（微架构/热点指令/缓存归因/代码结构/编译选项/汇编/算法/调用者上下文/多线程NUMA）' },
    { title: 'Synthesize', detail: '跨视角去重合并 + 交叉验证 + 互补发现 + 优先级排序' },
    { title: 'Audit', detail: '证据溯源审计（逐条核对 + 矛盾检测 + 置信度校准）' },
    { title: 'Apply', detail: '代码变更（向量化/展开/预取/分支消除/访存优化/编译选项/汇编）' },
    { title: 'Verify', detail: '编译 + 功能测试 + 性能对比 + git 操作' },
    { title: 'Fix', detail: '编译/功能错误修复（最多 10 轮）+ re-verify' }
  ]
}

// =============================================================================
// JSON Schemas — 定义所有阶段的结构化输出契约
// =============================================================================

// --- AnalyzeHotspot ---
const ANALYZE_HOTSPOT_SCHEMA = {
  type: 'object',
  properties: {
    function: { type: 'string' },
    source_file: { type: 'string' },
    lines: { type: 'array', items: { type: 'number' }, minItems: 2, maxItems: 2 },
    dynamic_analysis: {
      type: 'object',
      properties: {
        status: { type: 'string', enum: ['ok', 'partial', 'unavailable'] },
        perf_stat: {
          type: 'object',
          properties: {
            ipc: { type: 'number' },
            branch_mispredict_rate_pct: { type: 'number' },
            l1d_cache_miss_rate_pct: { type: 'number' },
            llc_cache_miss_rate_pct: { type: 'number' },
            cpu_clock_ms: { type: 'number' }
          }
        },
        perf_annotate_used: { type: 'boolean' },
        perf_annotate_top5: { type: 'array' },
        perf_spe_used: { type: 'boolean' },
        perf_spe_samples: { type: 'object' }
      }
    },
    static_analysis: {
      type: 'object',
      properties: {
        nested_loops: { type: 'number' },
        has_simd: { type: 'boolean' },
        simd_type: { type: ['string', 'null'] },
        current_parallelism: { type: ['number', 'null'] },
        data_dependencies: { type: 'string' },
        memory_access_pattern: { type: 'string' },
        stride_bytes: { type: 'number' },
        estimated_working_set_kb: { type: 'number' },
        branch_pattern: { type: 'string' },
        compiler_flags: { type: 'object' },
        instruction_query_evidence: { type: 'array' }
      }
    },
    optimization_points: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          type: { type: 'string' },
          sub_type: { type: ['string', 'null'] },
          target_arch: { type: 'string' },
          confidence: { type: 'number' },
          expected_speedup: { type: 'string' },
          priority: { type: 'number' },
          evidence: {
            type: 'object',
            properties: {
              static: { type: 'string' },
              dynamic: { type: 'string' }
            },
            required: ['static', 'dynamic']
          }
        },
        required: ['id', 'type', 'confidence', 'priority', 'evidence']
      }
    },
    skipped_points: { type: 'array' },
    pipeline_strategy: { type: 'object' },
    serial_chains: { type: 'array' },
    pipeline_utilization: { type: 'object' },
    status: { type: 'string', enum: ['analyzed', 'empty'] }
  },
  required: ['function', 'source_file', 'optimization_points', 'status']
}

// --- AdversarialReview（证据溯源审计）---
const AUDIT_SCHEMA = {
  type: 'object',
  properties: {
    audit_result: {
      type: 'object',
      properties: {
        optimization_point_id: { type: 'string' },
        status: { type: 'string', enum: ['confirmed', 'overturned', 'needs_revision'] },
        evidence_traces: { type: 'array' },
        contradictions: { type: 'array' },
        omissions: { type: 'array' },
        confidence_calibration: { type: 'object' },
        priority_issues: { type: 'array' }
      },
      required: ['optimization_point_id', 'status']
    }
  },
  required: ['audit_result']
}

// --- ApplyOptimization ---
const APPLY_SCHEMA = {
  type: 'object',
  properties: {
    function: { type: 'string' },
    optimization_point_id: { type: 'string' },
    strategy: { type: 'string' },
    status: { type: 'string', enum: ['applied', 'failed', 'compilation_failed'] },
    skill_used: { type: 'string' },
    optimization_success: { type: 'boolean' },
    modified_files: { type: 'array', items: { type: 'string' } },
    compilation: {
      type: 'object',
      properties: {
        attempted: { type: 'boolean' },
        ok: { type: 'boolean' },
        error: { type: ['string', 'null'] }
      }
    },
    smoke_test: { type: 'object' },
    error_message: { type: ['string', 'null'] },
    vectorization_result: { type: ['object', 'null'] },
    throughput_enhancement_result: { type: ['object', 'null'] },
    prefetch_optimization_result: { type: ['object', 'null'] },
    branch_elimination_result: { type: ['object', 'null'] },
    memory_access_result: { type: ['object', 'null'] },
    compiler_flag_result: { type: ['object', 'null'] },
    asm_optimization_result: { type: ['object', 'null'] },
    scalar_vector_hybrid_result: { type: ['object', 'null'] },
    generic_optimization_result: { type: ['object', 'null'] }
  },
  required: ['function', 'optimization_point_id', 'strategy', 'status'],
  additionalProperties: true
}

// --- VerifyOptimization ---
const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    function: { type: 'string' },
    optimization_point_id: { type: 'string' },
    compilation: {
      type: 'object',
      properties: {
        ok: { type: 'boolean' },
        warnings: { type: 'number' },
        error: { type: ['string', 'null'] }
      }
    },
    functional_test: {
      type: 'object',
      properties: {
        passed: { type: 'boolean' },
        details: { type: 'string' }
      }
    },
    performance: {
      type: 'object',
      properties: {
        execution_mode: { type: 'string' },
        baseline_metric: { type: 'number' },
        optimized_metric: { type: 'number' },
        speedup: { type: 'number' },
        regression: { type: 'boolean' },
        diagnostics: { type: 'object' }
      }
    },
    debug_process: { type: 'object' },
    regression_diagnosis: { type: 'object' },
    git: {
      type: 'object',
      properties: {
        committed: { type: 'boolean' },
        hash: { type: ['string', 'null'] },
        message: { type: 'string' }
      }
    },
    status: { type: 'string', enum: ['verified', 'marginal', 'regression', 'failed', 'unverified'] }
  },
  required: ['function', 'optimization_point_id', 'compilation', 'status']
}

// --- FixCode ---
const FIX_SCHEMA = {
  type: 'object',
  properties: {
    function: { type: 'string' },
    status: { type: 'string', enum: ['fixed', 'failed'] },
    iterations_used: { type: 'number' },
    error_type: { type: 'string', enum: ['compilation', 'functional', 'both'] },
    fixes_applied: { type: 'array' },
    remaining_errors: { type: 'array' },
    compilation: {
      type: 'object',
      properties: {
        ok: { type: 'boolean' },
        warnings: { type: 'number' },
        error: { type: ['string', 'null'] }
      }
    },
    functional_test: { type: 'object' },
    skipped_fixes: { type: 'array' }
  },
  required: ['function', 'status', 'iterations_used']
}

// =============================================================================
// 多视角分析 Schemas — 每个视角的轻量级输出
// =============================================================================

const PERSPECTIVE_MICROARCH_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    tma_used: { type: 'boolean' },
    bottleneck_type: { type: 'string', enum: ['compute_bound', 'memory_bound_l1', 'memory_bound_llc', 'branch_bound', 'frontend_bound', 'mixed', 'healthy'] },
    primary_bottleneck: { type: 'string' },
    secondary_bottleneck: { type: 'string' },
    severity: { type: 'string', enum: ['critical', 'moderate', 'mild', 'healthy'] },
    key_observations: { type: 'array', items: { type: 'string' } },
    microarch_calibration: {
      type: 'object',
      properties: {
        arch: { type: 'string' },
        cpu_part_id: { type: 'string' },
        theoretical_ipc: { type: 'number' },
        l1d_size_kb: { type: 'number' },
        l2_size_kb: { type: 'number' }
      }
    }
  },
  required: ['perspective', 'bottleneck_type', 'key_observations'],
  additionalProperties: true
}

const PERSPECTIVE_HOTINST_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    status: { type: 'string', enum: ['analyzed', 'degraded', 'unavailable'] },
    perf_annotate_used: { type: 'boolean' },
    objdump_available: { type: 'boolean' },
    perf_annotate_top10: { type: 'array' },
    hotspot_loop: { type: 'object' },
    theoretical_cycles: { type: 'object' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'status', 'key_observations'],
  additionalProperties: true
}

const PERSPECTIVE_CACHEMISS_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    status: { type: 'string', enum: ['analyzed', 'degraded', 'unavailable'] },
    spe_available: { type: 'boolean' },
    unavailable_reason: { type: ['string', 'null'] },
    top_cache_miss_instructions: { type: 'array' },
    top_tlb_miss_instructions: { type: 'array' },
    top_branch_miss_instructions: { type: 'array' },
    latency_distribution: { type: 'object' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'status'],
  additionalProperties: true
}

const PERSPECTIVE_CODESTRUCT_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    file_type: { type: 'string' },
    nested_loops: { type: 'number' },
    loop_bound_known: { type: 'boolean' },
    data_dependencies: { type: 'string' },
    accumulation_pattern: { type: 'object' },
    serial_chains: { type: 'array' },
    has_simd: { type: 'boolean' },
    simd_type: { type: ['string', 'null'] },
    current_parallelism: { type: ['number', 'null'] },
    deepen_opportunities: { type: 'array' },
    memory_access_pattern: { type: 'string' },
    stride_bytes: { type: 'number' },
    estimated_working_set_kb: { type: 'number' },
    cache_fit_breakdown: { type: 'object' },
    fits_in_cache: { type: 'string' },
    branch_pattern: { type: 'string' },
    branch_complexity: { type: 'string' },
    loop_invariants: { type: 'object' },
    microkernel_candidate: { type: 'boolean' },
    register_pressure_candidate: { type: 'boolean' },
    load_fma_overlap_candidate: { type: 'boolean' },
    load_fma_overlap_detail: { type: 'object' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'data_dependencies', 'memory_access_pattern', 'key_observations'],
  additionalProperties: true
}

const PERSPECTIVE_COMPILER_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    skill_used: { type: 'boolean' },
    compiler: { type: 'object' },
    flags_source: { type: 'string' },
    current_flags: { type: 'object' },
    autovec_diagnostic: { type: 'object' },
    suggestions: { type: 'array' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'suggestions', 'key_observations'],
  additionalProperties: true
}

const PERSPECTIVE_ASM_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    not_applicable: { type: 'boolean' },
    language: { type: 'string' },
    matched_domains: { type: 'array' },
    candidates: { type: 'array' },
    instruction_query_evidence: { type: 'array' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective'],
  additionalProperties: true
}

const PERSPECTIVE_ALGORITHM_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    status: { type: 'string', enum: ['analyzed', 'empty', 'degraded'] },
    identified_algorithm: { type: 'object' },
    search_summary: { type: 'object' },
    candidates: { type: 'array' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'status'],
  additionalProperties: true
}

const PERSPECTIVE_CALLER_CONTEXT_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    status: { type: 'string', enum: ['analyzed', 'empty', 'degraded'] },
    callers: { type: 'array' },
    optimization_points: { type: 'array' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'status'],
  additionalProperties: true
}

const PERSPECTIVE_THREADING_SCHEMA = {
  type: 'object',
  properties: {
    perspective: { type: 'string' },
    status: { type: 'string', enum: ['analyzed', 'not_applicable', 'degraded'] },
    trigger_reason: { type: 'string' },
    numa_analysis: { type: 'object' },
    lock_analysis: { type: 'object' },
    optimization_points: { type: 'array' },
    key_observations: { type: 'array', items: { type: 'string' } }
  },
  required: ['perspective', 'status'],
  additionalProperties: true
}

// 合成 Schema — 复用 ANALYZE_HOTSPOT_SCHEMA（输出格式兼容）

// =============================================================================
// 工具函数
// =============================================================================

/**
 * 构造 agent prompt：让 agent 自行 Read prompt 文件，
 * 并将动态上下文变量以 inline JSON 形式提供。
 *
 * promptFile: 相对于 prompt_root 的文件名，如 "perspective-microarch.md"
 * dynamicVars: { KEY: value } — agent 会用这些值替换文件中的 {{KEY}} 占位符
 */
function buildAgentPrompt(promptRoot, promptFile, dynamicVars) {
  var filePath = promptRoot + '/' + promptFile
  var varsJson = JSON.stringify(dynamicVars, null, 2)
  return [
    '在开始执行之前，Read 以下文件获取你的完整执行指令和输出格式：',
    '',
    '  ' + filePath,
    '',
    '该文件中的 {{PLACEHOLDER}} 占位符变量值如下，请在理解文件指令后自行替换：',
    '',
    '```json',
    varsJson,
    '```',
    '',
    '请严格按照文件中的执行步骤和输出格式完成任务。',
    '只输出符合 JSON Schema 的 JSON 对象，不要输出其他内容。'
  ].join('\n')
}

/**
 * 判断验证结果是否需要修复。
 */
function needsFix(ctx) {
  if (ctx.apply_failed) return true
  if (!ctx.verified) return false
  return ctx.verified.status === 'failed' || ctx.verified.status === 'regression'
}

// =============================================================================
// 主流程
// =============================================================================

// --- 输入适配：Orchestrator (LLM) 可能将 args 传为 JSON 字符串 ---
// Workflow 要求 args 是 JSON 对象，但 LLM 容易错误地写成字符串字面量。
// Object.keys 对 string 返回字符索引 [0, 1, 2, ...]，可作为检测信号。
var _rawArgs = args
if (typeof _rawArgs === 'string') {
  try {
    _rawArgs = JSON.parse(_rawArgs)
    log('检测到 args 为 JSON 字符串，已自动解析为对象。')
    args = _rawArgs
  } catch (_e) {
    throw new Error(
      'Workflow args 是字符串但无法解析为 JSON。' +
      'Orchestrator 必须传入 JSON 对象，不是字符串或 YAML 文本。' +
      '字符串前 500 字符: ' + String(_rawArgs).slice(0, 500)
    )
  }
}
if (_rawArgs === null || typeof _rawArgs !== 'object' || Array.isArray(_rawArgs)) {
  throw new Error(
    'Workflow args 类型错误: ' + (Array.isArray(_rawArgs) ? 'Array' : typeof _rawArgs) +
    '。必须传入 JSON 对象 { sub_tasks, prompt_root, prepareProject, ... }。'
  )
}

// --- 输入校验：检测 Orchestrator 是否遗漏必填字段 ---
const _requiredKeys = ['sub_tasks', 'prompt_root', 'prepareProject']
const _missingKeys = _requiredKeys.filter(function(k) { return _rawArgs[k] === undefined || _rawArgs[k] === null })
if (_missingKeys.length > 0) {
  var _keysList = Object.keys(_rawArgs).join(', ')
  throw new Error(
    'Workflow args 缺少必填字段: ' + _missingKeys.join(', ') +
    '。实际收到的 keys: [' + _keysList + ']。' +
    '请检查 Orchestrator 调用 Workflow 时的 args 构造。'
  )
}
if (!Array.isArray(_rawArgs.sub_tasks)) {
  throw new Error(
    'sub_tasks 必须是数组，实际类型: ' + typeof _rawArgs.sub_tasks +
    '，值: ' + JSON.stringify(_rawArgs.sub_tasks).slice(0, 200)
  )
}

const {
  run_id,
  round,
  sub_tasks,
  prompt_root: promptRoot,  // prompt 文件目录路径，agent 自行 Read 需要的文件
  prepareProject,
  intent,
  performanceProfile,
  testMethod
} = _rawArgs

if (sub_tasks.length === 0) {
  log('本轮无子任务，直接返回空结果')
  // pipeline() 不接受空数组，提前返回
  return {
    round: round || 0,
    sub_task_results: [],
    optimization_points_total: 0,
    applied_count: 0,
    skipped_count: 0,
    failed_count: 0,
    unresolved_count: 0
  }
}

log(`第 ${round} 轮优化开始，共 ${sub_tasks.length} 个子任务`)

// =============================================================================
// 阶段 1: 函数级多视角分析（9 视角并行 → 合成）
// =============================================================================

phase('Analyze')

const analyzeResults = await pipeline(
  sub_tasks,

  // --- Stage 1a: 多视角并行分析 + 合成 ---
  async function(task) {
    const context = {
      repo: prepareProject.repo,
      target: {
        source_files: [task.source_file],
        entry_functions: [task.function]
      },
      baseline: prepareProject.baseline,
      binary_path: prepareProject.binary_path,
      sub_task: task,
      test_method: testMethod,
      intent: intent,
      performance_profile: performanceProfile,
      architecture_file: prepareProject.architecture_file,
      microarch_file: prepareProject.microarch_file,
      instruction_perf_file: prepareProject.instruction_perf_file
    }

    // ---- 视角 1-2: 始终执行（微架构事件 + 热点指令）----
    var basePerspectives = []

    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-microarch.md', {
        CONTEXT: context,
        TEST_METHOD: testMethod
      })
      return agent(prompt, {
        schema: PERSPECTIVE_MICROARCH_SCHEMA,
        label: 'p1-microarch:' + task.function,
        phase: 'Analyze'
      })
    })

    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-hot-inst.md', {
        CONTEXT: context,
        TEST_METHOD: testMethod,
        FUNCTION_NAME: task.function
      })
      return agent(prompt, {
        schema: PERSPECTIVE_HOTINST_SCHEMA,
        label: 'p2-hotinst:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 3: 缓存/分支归因（SPE agent 内部自行降级）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-cache-miss.md', {
        CONTEXT: context,
        TEST_METHOD: testMethod,
        FUNCTION_NAME: task.function
      })
      return agent(prompt, {
        schema: PERSPECTIVE_CACHEMISS_SCHEMA,
        label: 'p3-cachemiss:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 4: 代码结构（始终执行）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-code-struct.md', {
        CONTEXT: context
      })
      return agent(prompt, {
        schema: PERSPECTIVE_CODESTRUCT_SCHEMA,
        label: 'p4-codestruct:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 5: 编译选项（始终执行）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-compiler.md', {
        CONTEXT: context
      })
      return agent(prompt, {
        schema: PERSPECTIVE_COMPILER_SCHEMA,
        label: 'p5-compiler:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 6: 汇编指令级（条件触发：.s/.S 文件）----
    var sourceFile = task.source_file || ''
    var isAsm = sourceFile.endsWith('.s') || sourceFile.endsWith('.S')
    if (isAsm) {
      basePerspectives.push(function() {
        var prompt = buildAgentPrompt(promptRoot, 'perspective-asm.md', {
          CONTEXT: context
        })
        return agent(prompt, {
          schema: PERSPECTIVE_ASM_SCHEMA,
          label: 'p6-asm:' + task.function,
          phase: 'Analyze'
        })
      })
    }

    // ---- 视角 7: 算法模式（始终执行，内部通过语义分析判断是否触发）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-algorithm.md', {
        CONTEXT: context
      })
      return agent(prompt, {
        schema: PERSPECTIVE_ALGORITHM_SCHEMA,
        label: 'p7-algorithm:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 8: 调用者上下文（始终执行，内部有触发条件判断）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-caller-context.md', {
        CONTEXT: context
      })
      return agent(prompt, {
        schema: PERSPECTIVE_CALLER_CONTEXT_SCHEMA,
        label: 'p8-caller:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 视角 9: 多线程与 NUMA（始终执行，内部有单线程退出判断）----
    basePerspectives.push(function() {
      var prompt = buildAgentPrompt(promptRoot, 'perspective-threading.md', {
        CONTEXT: context,
        TEST_METHOD: testMethod
      })
      return agent(prompt, {
        schema: PERSPECTIVE_THREADING_SCHEMA,
        label: 'p9-threading:' + task.function,
        phase: 'Analyze'
      })
    })

    // ---- 并行启动所有视角 ----
    if (basePerspectives.length === 0) {
      log(task.function + ': 无可用的分析视角，跳过')
      return null
    }

    log(task.function + ': 启动 ' + basePerspectives.length + ' 个视角并行分析...')

    var findings = await parallel(basePerspectives)
    var validFindings = findings.filter(function(f) { return f !== null })

    if (validFindings.length === 0) {
      log(task.function + ': 所有视角分析失败，跳过')
      return null
    }

    // 统计各视角结果
    var perspectiveNames = validFindings.map(function(f) { return f.perspective || 'unknown' })
    log(task.function + ': ' + validFindings.length + '/' + basePerspectives.length +
      ' 个视角返回结果: ' + perspectiveNames.join(', '))

    // ---- 合成阶段 ----

    var synthPrompt = buildAgentPrompt(promptRoot, 'synthesize.md', {
      FINDINGS: validFindings,
      SUB_TASK: task,
      INTENT: intent,
      FUNCTION_NAME: task.function,
      SOURCE_FILE: task.source_file,
      LINES: task.lines || [0, 0]
    })

    var synthesis = await agent(synthPrompt, {
      schema: ANALYZE_HOTSPOT_SCHEMA,
      label: 'synthesize:' + task.function,
      phase: 'Synthesize'
    })

    if (!synthesis) {
      log(task.function + ': 合成失败，使用视角 4 的静态分析结果')
      return { task: task, analysis: validFindings[0], context: context, findings: validFindings }
    }

    log(task.function + ': 合成完成，发现 ' +
      (synthesis.optimization_points ? synthesis.optimization_points.length : 0) + ' 个优化点')

    // 附加原始 findings 供调试追溯
    synthesis._findings = validFindings

    return { task: task, analysis: synthesis, context: context }
  }
)

// =============================================================================
// 阶段 2: 优化点级处理（per-function pipeline）
//   Decide → Challenge → Apply → Verify → [Fix → Re-Verify]
// =============================================================================

const subTaskResults = []

// 逐函数处理（每个函数的优化点串行，但多个函数已在上层 pipeline 中并行分析）
for (const ctx of analyzeResults.filter(function(x) { return x !== null })) {
  const task = ctx.task
  const analysis = ctx.analysis

  if (!analysis || analysis.status === 'empty' ||
      !analysis.optimization_points || analysis.optimization_points.length === 0) {
    log(task.function + ': 无优化点，跳过')
    subTaskResults.push({
      id: task.id,
      function: task.function,
      status: 'skipped',
      speedup: null,
      fix_info: '',
      description: task.reason || '',
      optimization_point_results: []
    })
    continue
  }

  log(task.function + ': 开始处理 ' + analysis.optimization_points.length + ' 个优化点')

  // --- 优化点级串行处理（按优先级排序，逐点 audit→apply→verify→[fix]）---
  var sortedPoints = analysis.optimization_points.slice().sort(function(a, b) {
    return a.priority - b.priority
  })

  log(task.function + ': 开始串行处理 ' + sortedPoints.length + ' 个优化点（按优先级排序）')

  const pointResults = []

  for (const point of sortedPoints) {
    log(task.function + ': [' + point.id + '] 开始 (type=' + point.type + ', priority=' + point.priority + ')')

    // === Stage 2a: AdversarialReview（证据溯源审计）===

    if (point.type === 'algorithm-substitution' || point.type === 'math-rewrite') {
      log(point.id + ': ' + point.type + ' → 自动 skip（需人工验证）')
      pointResults.push(null)
      continue
    }

    var auditPrompt = buildAgentPrompt(promptRoot, 'adversarial-review.md', {
      OPT_POINT: point,
      SYNTHESIS: analysis,
      FINDINGS: analysis._findings || []
    })

    var audit = await agent(auditPrompt, {
      schema: AUDIT_SCHEMA,
      label: 'audit:' + task.function + ':' + point.id,
      phase: 'Audit'
    })

    if (!audit || !audit.audit_result) {
      log(point.id + ': 审计失败（agent 返回 null），跳过')
      pointResults.push(null)
      continue
    }

    var result = audit.audit_result

    if (result.status === 'overturned') {
      log(point.id + ': 审计推翻 → ' + (result.contradictions || []).length + ' 个矛盾')
      pointResults.push(null)
      continue
    }

    if (result.status === 'needs_revision') {
      if (result.confidence_calibration && result.confidence_calibration.recommended) {
        point.confidence = result.confidence_calibration.recommended
      }
      if (result.priority_issues && result.priority_issues.length > 0) {
        var suggestedPriority = result.priority_issues[0].suggested_priority
        if (suggestedPriority) point.priority = suggestedPriority
      }
      log(point.id + ': 审计需修正 → confidence=' + point.confidence + ', priority=' + point.priority)
    } else {
      log(point.id + ': 审计通过 → ' + (result.evidence_traces || []).length + ' 条证据溯源一致')
    }

    // === Stage 2b: ApplyOptimization ===

    var decision = {
      function: analysis.function,
      optimization_point_id: point.id,
      strategy: point.type,
      skill: point.type,
      arch: point.target_arch || 'neon',
      confidence: point.confidence,
      expected_speedup: point.expected_speedup,
      risk: 'medium',
      input: {
        source_file: analysis.source_file,
        function: analysis.function,
        lines: analysis.lines || [0, 0],
        target_arch: point.target_arch || 'neon',
        language: 'c_cpp',
        sub_type: point.sub_type || null,
        optimization_type: null,
        microkernel_hint: null,
        diagnostics: { register_pressure_analysis_required: false, load_fma_overlap_candidate: false }
      },
      throughput_enhancement: null,
      pipeline_strategy: analysis.pipeline_strategy || null,
      status: 'confirmed'
    }

    var applyPrompt = buildAgentPrompt(promptRoot, 'apply-optimization.md', {
      DECISION: decision,
      PREPARE_PROJECT: prepareProject,
      ANALYZE_HOTSPOT_RESULT: analysis
    })

    var applied = await agent(applyPrompt, {
      schema: APPLY_SCHEMA,
      label: 'apply:' + task.function + ':' + point.id,
      phase: 'Apply'
    })

    var applyFailed = false
    if (!applied) {
      log(point.id + ': 应用失败（agent 返回 null）')
      applyFailed = true
    } else if (applied.status === 'failed' || applied.status === 'compilation_failed') {
      log(point.id + ': 应用/编译失败 → ' + applied.status)
      applyFailed = true
    } else {
      log(point.id + ': 应用成功 → ' + (applied.modified_files || []).join(', '))
    }

    // === Stage 2c: VerifyOptimization ===

    var verified = null
    if (!applyFailed) {
      var verifyPrompt = buildAgentPrompt(promptRoot, 'verify-optimization.md', {
        APPLIED: applied,
        DECISION: decision,
        REPO: prepareProject.repo,
        BASELINE: prepareProject.baseline,
        FIX_CODE: null
      })

      verified = await agent(verifyPrompt, {
        schema: VERIFY_SCHEMA,
        label: 'verify:' + task.function + ':' + point.id,
        phase: 'Verify'
      })

      if (verified) {
        var perfInfo = verified.performance
          ? (' speedup=' + (verified.performance.speedup || 'N/A'))
          : ''
        log(point.id + ': 验证 → ' + verified.status + perfInfo)
      } else {
        log(point.id + ': 验证失败（agent 返回 null）')
      }
    }

    // === Stage 2d: FixCode + Re-Verify ===

    var fixResult = null
    var reVerified = null

    if (applyFailed || (verified && (verified.status === 'failed' || verified.status === 'regression'))) {
      var maxRounds = 10
      for (var fixRound = 0; fixRound < maxRounds; fixRound++) {
        var fixPrompt = buildAgentPrompt(promptRoot, 'fix-code.md', {
          VERIFIED: verified || {},
          APPLIED: applied || {},
          DECISION: decision,
          REPO: prepareProject.repo,
          BASELINE: prepareProject.baseline
        })

        fixResult = await agent(fixPrompt, {
          schema: FIX_SCHEMA,
          label: 'fix:' + task.function + ':' + point.id + ':r' + (fixRound + 1),
          phase: 'Fix'
        })

        if (!fixResult) break

        log(point.id + ': 修复第' + (fixRound + 1) + '轮 → ' + fixResult.status)

        if (fixResult.status === 'fixed') break
      }

      if (fixResult && fixResult.status === 'fixed') {
        var reVerifyPrompt = buildAgentPrompt(promptRoot, 'verify-optimization.md', {
          APPLIED: applied,
          DECISION: decision,
          REPO: prepareProject.repo,
          BASELINE: prepareProject.baseline,
          FIX_CODE: fixResult
        })

        reVerified = await agent(reVerifyPrompt, {
          schema: VERIFY_SCHEMA,
          label: 're-verify:' + task.function + ':' + point.id,
          phase: 'Verify'
        })

        if (reVerified) {
          log(point.id + ': 重新验证 → ' + reVerified.status)
        }
      } else if (fixResult && fixResult.status === 'failed') {
        log(point.id + ': 修复耗尽（10 轮），标记 unresolved')
      }
    }

    pointResults.push({
      point: point,
      audit: audit,
      decision: decision,
      applied: applied,
      apply_failed: applyFailed,
      verified: verified,
      fixResult: fixResult,
      reVerified: reVerified
    })

    log(task.function + ': [' + point.id + '] 完成')
  }

  // =========================================================================
  // 汇总本函数的优化点结果
  // =========================================================================

  var validResults = pointResults.filter(function(r) { return r !== null })
  var applied = validResults.filter(function(r) {
    return r.verified && (r.verified.status === 'verified' || r.verified.status === 'marginal')
  })
  var failed = validResults.filter(function(r) {
    return !r.verified || r.verified.status === 'failed' || r.verified.status === 'regression'
  })
  var unresolved = validResults.filter(function(r) {
    return r.fixResult && r.fixResult.status === 'failed' && !r.reVerified
  })

  // 综合状态：取最差结果
  var status = 'verified'
  if (unresolved.length > 0) {
    status = 'unresolved'
  } else if (failed.length > 0) {
    status = 'failed'
  } else if (validResults.every(function(r) {
    return !r.decision || r.decision.status === 'skipped'
  })) {
    status = 'skipped'
  } else if (applied.length === 0 && validResults.length > 0) {
    status = 'unverified'
  }

  var speedups = applied
    .map(function(r) { return r.verified && r.verified.performance ? r.verified.performance.speedup : null })
    .filter(function(s) { return s !== null && s > 0 })
    .sort(function(a, b) { return b - a })
  var bestSpeedup = speedups.length > 0 ? speedups[0] : null

  var fixRounds = validResults.filter(function(r) { return r.fixResult }).length

  subTaskResults.push({
    id: task.id,
    function: task.function,
    status: status,
    speedup: bestSpeedup ? bestSpeedup.toFixed(1) + 'x' : null,
    fix_info: fixRounds > 0 ? '修复' + fixRounds + '轮' : '',
    description: task.reason || '',
    optimization_point_results: validResults.map(function(r) {
      var verifiedStatus = 'skipped'
      if (r.reVerified) {
        verifiedStatus = r.reVerified.status
      } else if (r.verified) {
        verifiedStatus = r.verified.status
      } else if (r.apply_failed) {
        verifiedStatus = 'failed'
      }

      var speedupStr = null
      var perf = (r.reVerified || r.verified || {}).performance
      if (perf && perf.speedup) {
        speedupStr = perf.speedup.toFixed(1) + 'x'
      }

      return {
        optimization_point_id: r.point ? r.point.id : 'unknown',
        type: r.point ? r.point.type : 'unknown',
        strategy: r.decision ? r.decision.strategy : 'unknown',
        status: verifiedStatus,
        speedup: speedupStr,
        fix_rounds: r.fixResult ? r.fixResult.iterations_used : 0,
        decision: r.decision || null,
        applied: r.applied || null,
        verified: r.reVerified || r.verified || null,
        fix_result: r.fixResult || null
      }
    })
  })

  log(task.function + ': 完成 → ' + status +
    ' (' + applied.length + ' applied, ' + failed.length + ' failed, ' +
    unresolved.length + ' unresolved)')
}

// =============================================================================
// 返回结果
// =============================================================================

var totalPoints = subTaskResults.reduce(
  function(sum, s) { return sum + s.optimization_point_results.length }, 0
)
var appliedCount = subTaskResults.filter(
  function(s) { return s.status === 'verified' || s.status === 'marginal' }
).length
var skippedCount = subTaskResults.filter(
  function(s) { return s.status === 'skipped' }
).length
var failedCount = subTaskResults.filter(
  function(s) { return s.status === 'failed' || s.status === 'unresolved' }
).length
var unresolvedCount = subTaskResults.filter(
  function(s) { return s.status === 'unresolved' }
).length

log('第 ' + round + ' 轮完成: ' + totalPoints + ' 优化点, ' +
  appliedCount + ' applied, ' + skippedCount + ' skipped, ' +
  failedCount + ' failed (' + unresolvedCount + ' unresolved)')

return {
  round: round,
  sub_task_results: subTaskResults,
  optimization_points_total: totalPoints,
  applied_count: appliedCount,
  skipped_count: skippedCount,
  failed_count: failedCount,
  unresolved_count: unresolvedCount
}
