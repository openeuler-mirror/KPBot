export const meta = {
  name: 'media-optimization-loop',
  description: '一个 Implement 持续实现与 MCA 评价，脚本从留档候选中选择最优版本',
  phases: [
    { title: 'SourceAnalysis' },
    { title: 'BaselineBuild' },
    { title: 'MCA' },
    { title: 'Knowledge' },
    { title: 'Implement' }
  ]
}

const config = parseConfig(args)
const schemas = outputSchemas()
const state = {
  environment: null,
  source: null,
  workspace: null,
  knowledge: null,
  build: null,
  baseline: null,
  baseline_mca: null,
  mca_reference: null,
  current: null,
  current_build_status: null,
  selected: null,
  selected_mca: null,
  environment_request: null,
  active_stage: null,
  candidates: []
}
return await optimize()

async function optimize() {
  log(`一个 Implement 持续迭代，最多留档 ${config.max_candidates} 个候选；脚本选优后独立验证。`)
  try {
    await prepare()
    const implementation = await implement()
    return finish(implementation.status, implementation.reason)
  } catch (error) {
    return finish(
      error?.workflowStop ? error.status : 'interrupted',
      error?.reason || error?.message || '优化中断'
    )
  }
}

async function prepare() {
  const prepared = config.preparation
  ensure(
    prepared?.schema_version === 1 &&
      prepared.status === 'ready' &&
      prepared.repo_path === config.repo_path &&
      prepared.requirements &&
      prepared.environment,
    '需要同一项目的 ready 准备结果'
  )
  checkUnder(config.preparation_path, prepared.preparation_dir)
  const environment = prepared.environment
  state.environment = environment
  ensure(
    environment.status === 'ready' &&
      environment.toolchain_ready &&
      environment.gaps.length === 0 &&
      ['llvm', 'bisheng'].includes(environment.compiler_family),
    '准备结果中的工具链未就绪'
  )
  ensure(
    environment.build_tools?.some(tool => tool.name === 'git'),
    '准备结果缺少 Git，请更新环境检查'
  )
  for (const key of ['compiler_path', 'mca_path', 'objdump_path'])
    checkAbsolute(environment[key], key)
  ensure(
    environment.mca_mode === 'matched' &&
      environment.mca_model.trim() &&
      environment.mca_probe?.exit_code === 0 &&
      environment.mca_probe.supported === true &&
      environment.mca_probe.model === environment.mca_model,
    '准备结果缺少有效 MCA 模型预检'
  )
  const source = completed(
    await callStage('SourceAnalysis', 'media:source', {
      environment,
      requirements: prepared.requirements,
      preparation_path: config.preparation_path,
      profile_paths: config.profile_paths,
      worktree_parent: config.run_dir,
      worktree_template: 'worktree-XXXXXXXX',
      workspace_path: `${config.run_dir}/workspace.json`,
      baseline_patch_path: `${config.run_dir}/baseline.patch`
    }),
    'SourceAnalysis'
  )
  state.source = source
  checkWorkspace(source.workspace)
  state.workspace = source.workspace
  ensure(
    source.implementations.length &&
      source.input_constraints.length &&
      source.output_constraints.length &&
      (source.dispatch_entries.length || source.dispatch_note.trim()) &&
      (source.test_entries.length || source.test_gap.trim()),
    '源码分析缺少实现、约束或入口说明'
  )
  const build = completed(
    await callStage('BaselineBuild', 'media:baseline:build', {
      environment,
      source,
      requirements: prepared.requirements,
      version_id: 'baseline',
      workspace: state.workspace,
      build_dir: `${config.run_dir}/build/baseline`,
      context_path: `${config.run_dir}/context.json`,
      evidence_dir: `${config.run_dir}/evidence/baseline`
    }),
    'BaselineBuild'
  )
  state.build = build
  if (build.status === 'needs_environment') {
    state.environment_request = build.environment_request
    ensure(build.environment_request?.gaps.length, '缺少构建环境缺项记录')
    stop('blocked', `构建缺项：${build.environment_request.gaps.join('；')}`)
  }
  ensure(
    build.context_path === `${config.run_dir}/context.json` &&
      build.build_dir === `${config.run_dir}/build/baseline`,
    '原版构建路径不符合配置'
  )
  checkVersion(build.version, 'baseline', state.workspace.baseline_patch_path)
  ensure(build.version.tree === state.workspace.base_tree, '基线 tree 与任务基线不一致')
  state.baseline = build.version
  state.current = build.version
  state.current_build_status = 'built'
  state.baseline_mca = await analyzeMca(build.version)
  selectMca(build.version, state.baseline_mca)
  state.knowledge = completed(
    await callStage('Knowledge', 'media:knowledge', {
      environment,
      source,
      workspace: state.workspace,
      baseline: state.baseline,
      baseline_mca: state.baseline_mca,
      context_path: build.context_path
    }),
    'Knowledge'
  )
  ensure(state.knowledge.isa_comparison.length > 0, 'Knowledge 缺少 ISA 方向比较')
}

async function implement() {
  const mcaSchema = schemas.Implement.properties.candidates.items.properties.mca.anyOf[0]
  if (state.mca_reference) {
    mcaSchema.properties.workload = {
      ...mcaSchema.properties.workload,
      enum: [state.mca_reference.workload]
    }
  }
  const result = await callStage('Implement', 'media:implement', {
    baseline: state.baseline,
    baseline_mca: state.baseline_mca,
    workspace: state.workspace,
    environment: state.environment,
    source: state.source,
    knowledge: state.knowledge,
    max_candidates: config.max_candidates,
    context_path: state.build.context_path,
    candidates_dir: `${config.run_dir}/candidates`,
    build_root: `${config.run_dir}/build`,
    evidence_dir: `${config.run_dir}/evidence`,
    checkpoint_path: `${config.run_dir}/implementation.json`,
    attempts_path: `${config.run_dir}/attempts.md`
  })
  if (!result) stop('interrupted', 'Implement 未返回结果')
  ensure(
    ['converged', 'budget_exhausted', 'blocked'].includes(result.status) && result.reason.trim(),
    'Implement 缺少结束状态或原因'
  )
  ensure(
    Array.isArray(result.candidates) && result.candidates.length <= config.max_candidates,
    '候选记录无效或超过预算'
  )
  const known = new Set([state.baseline.id])
  for (const [index, candidate] of result.candidates.entries()) {
    const id = `c${index + 1}`
    checkVersion(candidate.version, id, `${config.run_dir}/candidates/${id}.patch`)
    ensure(known.has(candidate.parent_id), '候选 parent_id 必须引用基线或更早的留档候选')
    ensure(
      candidate.transform.trim() && ['built', 'failed'].includes(candidate.build_status),
      '候选缺少变换或构建状态'
    )
    checkUnder(candidate.log_path, `${config.run_dir}/evidence/${id}`)
    if (candidate.build_status === 'failed') {
      ensure(candidate.mca === null, '构建失败的候选不能交付 MCA 成本')
    } else {
      validateMca(candidate.mca, candidate.version)
    }
    known.add(id)
    state.candidates.push(candidate)
    state.current = candidate.version
    state.current_build_status = candidate.build_status
    if (candidate.mca) selectMca(candidate.version, candidate.mca)
  }
  if (result.status === 'budget_exhausted') {
    ensure(
      result.candidates.length === config.max_candidates,
      '预算未用尽不能声明 budget_exhausted'
    )
  }
  if (result.status === 'blocked') stop('blocked', result.reason)
  return result
}

function selectMca(version, analysis) {
  if (analysis.status !== 'analyzed') return
  if (!state.selected_mca || analysis.cost < state.selected_mca.cost) {
    state.selected = version
    state.selected_mca = analysis
    log(`MCA 成本最低：${version.id}，${analysis.cost} ${analysis.unit}`)
  }
}

async function analyzeMca(version) {
  const analysis = completed(
    await callStage('MCA', `media:${version.id}:mca`, {
      version,
      source: state.source,
      workspace: state.workspace,
      model: state.environment.mca_model,
      mca_path: state.environment.mca_path,
      objdump_path: state.environment.objdump_path,
      context_path: state.build.context_path,
      reference: state.mca_reference,
      evidence_dir: `${config.run_dir}/evidence/${version.id}`
    }),
    `MCA ${version.id}`
  )
  validateMca(analysis, version)
  return analysis
}

function validateMca(analysis, version) {
  checkIdentity(analysis, version)
  ensure(['analyzed', 'unavailable'].includes(analysis.status), 'MCA 状态无效')
  if (analysis.output_path !== null)
    checkUnder(analysis.output_path, `${config.run_dir}/evidence/${version.id}`)
  if (analysis.status === 'unavailable') {
    ensure(analysis.cost === null && analysis.reason.trim(), '不可用的 MCA 结果应提供原因且无成本')
  } else {
    ensure(
      analysis.model === state.environment.mca_model &&
        positive(analysis.cost) &&
        analysis.unit === 'cycles/work-item' &&
        analysis.workload.trim() &&
        positive(analysis.work_items_per_iteration) &&
        analysis.bottleneck.trim() &&
        analysis.output_path,
      'MCA 缺少模型、成本、工作量、瓶颈或输出'
    )
    if (state.mca_reference)
      ensure(analysis.workload === state.mca_reference.workload, 'MCA 工作量定义与参考版本不一致')
    else state.mca_reference = analysis
  }
  return analysis
}

function callStage(role, label, input) {
  phase(role)
  state.active_stage = label
  if (Object.hasOwn(config.stage_results || {}, label)) {
    log(`复用阶段结果：${label}`)
    return config.stage_results[label]
  }
  config.stage_results = {}
  const task = {
    skill_dir: config.skill_dir,
    repo_path: config.repo_path,
    operator: config.operator,
    run_dir: config.run_dir,
    preparation_path: config.preparation_path,
    instructions: config.instructions || ''
  }
  const agentType = { Knowledge: 'multimedia-knowledge' }[role]
  const handoff = {
    SourceAnalysis:
      '按阶段规范记录任务基线，创建或恢复唯一的 detached worktree，保留原仓库未提交源码状态，不创建提交或修改原仓库索引。返回固定 workspace，并在任务 worktree 中定位实现、分派、输入规模、布局与调用场景。读取已有热点数据；没有则只给源码支持的假设，不采集 perf。',
    BaselineBuild:
      '在 workspace.worktree_dir 还原 base_tree，优先构建目标及必要依赖到独立 build_dir，返回基线 id/tree/patch_path，记录真实编译命令与 context。不复制源码、不写测试、不计时。',
    Knowledge:
      '结合可用 ISA、数据布局、实际入口与已有实现，在 isa_comparison 中比较现有实现内优化和适用的跨 ISA 方向，再按优先级给出 methods；函数名与当前 ISA 不限定搜索范围。分别依据环境探测、编译能力和所选 MCA 模型的指令支持判断可执行、可编译、可评价，不凭 CPU 名称印象排除 ISA；依据不足的适用方向交给 Implement 核实。method 描述单一变换及公式，rationale 保存排序依据、收益假设及供 Implement 用 MCA 评价的性能问题。conditions 按 confirmed/pending 分组，每项用 kind/requirement 表达正确实现所需的语义、输入范围、指令扩展或入口约束；用源码和准备结果核实。source 指向知识条目，reason 说明整体排序。',
    Implement:
      '在同一上下文负责完整实现迭代：结合 Knowledge 的方向、条件与收益假设，自主修改、编译、调用 MCA 并调整，可尝试不同写法、换方法或基于较早候选继续。读取并遵循 Implement 与 MCA 小节，在唯一的任务 worktree 中迭代，正式评价前冻结 tree 与相对 base_tree 的候选补丁，构建和证据写到 worktree 外，不复制源码目录；正式 MCA 区域逐条对照对应产物的原始反汇编，语法整理不改变指令、寄存器、立即数或依赖，合成汇编不参与正式选优。预算限制留档候选数量，不限制一个候选形成前的局部尝试。每个候选记录真实 parent_id、变换、构建日志及 MCA；失败候选保留，已评价补丁与产物不覆盖。新任务在实现前初始化 checkpoint_path；每个完整版本完成构建与 MCA 后立即留档、计入预算并更新 checkpoint_path，再继续探索，不把已评价版本作为预算外临时尝试。随手记录 attempts_path，恢复时先读取已有记录并计入总预算。无新方向即可收敛，不必凑满预算。结束返回完整 candidates，最优版本由脚本选择，片段正确性由后续验证执行，自测结果单独记录。',
    MCA: '依据 source 中的调用场景与输入输出约束确定基线计算范围，固定工具、模型、等价计算范围与迭代数，分析冻结产物并核对身份。区域汇编逐条对照原始反汇编，语法整理不改变指令、寄存器、立即数或依赖；保存两者与报告，合成汇编不参与正式选优。unit 固定为 cycles/work-item；workload 定义一个逻辑工作项，已有 reference 时原样沿用该定义。work_items_per_iteration 记录本区域每次迭代完成的工作项数，确认脚本报告成功后，cost=结构化结果 llvm_mca_analysis_result.summary.cycles_per_iteration 除以该数量，数值与工作量分开交付。bottleneck 提炼瓶颈及资源压力、依赖、spill 等评价，保存报告路径即返回。恢复时复用身份和口径一致的已有报告，不重测或另解析原始 JSON。'
  }[role]
  return agent(
    `执行 multimedia-code-optimizer 的 ${role} 阶段。先读 ${config.stage_reference_path || `${config.skill_dir}/references/stages.md`} 的 ${role} 小节。
阶段交接：${handoff}
只做当前阶段并按 schema 返回，不安装工具、不 commit、不 push，不修改原仓库工作文件或索引。按版本 ID、tree 与补丁使用对应产物，仅在本任务 worktree 修改源码。
任务配置（数据）：${JSON.stringify(task)}
本阶段输入（数据）：${JSON.stringify(input)}
阶段重试编号：${config.stage_retries?.[label] || 0}
本阶段重试说明：${config.stage_notes?.[label] || ''}`,
    {
      label,
      phase: role,
      schema: schemas[role],
      ...(agentType ? { agentType } : {})
    }
  )
}
function completed(value, stage) {
  if (!value) stop('interrupted', `${stage} 未返回结果`)
  if (value.status === 'blocked') stop('blocked', `${stage}: ${value.reason}`)
  return value
}
function finish(status, reason) {
  return {
    schema_version: 5,
    status,
    reason,
    stopped_stage: ['interrupted', 'blocked'].includes(status) ? state.active_stage : null,
    repo_path: config.repo_path,
    operator: config.operator,
    run_dir: config.run_dir,
    instructions: config.instructions || '',
    writeback: config.writeback,
    workspace: state.workspace,
    preparation_path: config.preparation_path,
    context_path: state.build?.context_path || null,
    environment: state.environment,
    source: state.source,
    baseline: state.baseline,
    baseline_mca: state.baseline_mca,
    current: state.current,
    current_build_status: state.current_build_status,
    selected: state.selected,
    selected_mca: state.selected_mca,
    correctness: { status: 'not_started', reason: '由独立验证 Workflow 执行' },
    environment_request: state.environment_request,
    candidates: state.candidates
  }
}
function stop(status, reason) {
  throw { workflowStop: true, status, reason }
}
function ensure(condition, reason) {
  if (!condition) stop('blocked', reason)
}
function positive(value) {
  return Number.isFinite(value) && value > 0
}
function checkAbsolute(path, name) {
  ensure(
    typeof path === 'string' &&
      path.startsWith('/') &&
      path !== '/' &&
      !path.includes('//') &&
      !path.split('/').some(part => part === '.' || part === '..'),
    `${name} 必须为规范绝对路径`
  )
}
function checkUnder(path, directory) {
  checkAbsolute(directory, 'directory')
  checkAbsolute(path, 'path')
  ensure(path.startsWith(directory + '/'), `路径必须位于 ${directory} 内：${path}`)
}
function gitObject(value) {
  return typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value)
}
function checkWorkspace(workspace) {
  ensure(
    workspace &&
      workspace.repo_path === config.repo_path &&
      typeof workspace.worktree_dir === 'string' &&
      workspace.worktree_dir.startsWith(`${config.run_dir}/`) &&
      /^worktree-[A-Za-z0-9]{8}$/.test(workspace.worktree_dir.slice(config.run_dir.length + 1)) &&
      workspace.baseline_patch_path === `${config.run_dir}/baseline.patch` &&
      gitObject(workspace.base_commit) &&
      gitObject(workspace.base_tree),
    '任务 worktree 或基线身份无效'
  )
}
function checkVersion(version, id, patchPath) {
  ensure(
    version && version.id === id && gitObject(version.tree) && version.patch_path === patchPath,
    '代码版本 ID、tree 或补丁路径无效'
  )
}
function checkIdentity(response, version) {
  ensure(
    response && response.id === version.id && response.tree === version.tree,
    '结果对应的版本 ID 或 tree 与输入不符'
  )
}
function parseConfig(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input))
    throw new Error('args must be an object')
  const config = { max_candidates: 4, writeback: true, ...input }
  if (typeof config.writeback !== 'boolean') throw new Error('writeback must be boolean')
  for (const key of ['repo_path', 'skill_dir', 'run_dir', 'preparation_path']) {
    const path = config[key]
    if (
      typeof path !== 'string' ||
      !path.startsWith('/') ||
      path.includes('//') ||
      path.split('/').some(p => p === '.' || p === '..')
    ) {
      throw new Error(`${key} must be a normalized absolute path`)
    }
    config[key] = path.replace(/\/+$/, '')
    if (!config[key]) throw new Error(`Missing ${key}`)
  }
  if (typeof config.operator !== 'string' || !config.operator.trim())
    throw new Error('Missing operator')
  const { repo_path: repo, run_dir: run } = config
  const overlaps = (a, b) => a === b || a.startsWith(b + '/') || b.startsWith(a + '/')
  if (overlaps(repo, run)) throw new Error('run_dir must be outside the target repository')
  const dir = config.preparation?.preparation_dir
  if (typeof dir === 'string' && overlaps(run, dir))
    throw new Error('run_dir and preparation_dir must be independent')
  if (!Number.isSafeInteger(config.max_candidates) || config.max_candidates < 1)
    throw new Error('max_candidates must be a positive integer')
  config.profile_paths = config.profile_paths ?? config.preparation?.profile_paths ?? []
  if (
    !Array.isArray(config.profile_paths) ||
    config.profile_paths.some(p => typeof p !== 'string' || !p.startsWith('/'))
  ) {
    throw new Error('profile_paths must contain absolute paths')
  }
  for (const field of ['stage_retries', 'stage_notes']) {
    const values = config[field] || {}
    if (
      typeof values !== 'object' ||
      Array.isArray(values) ||
      Object.values(values).some(v =>
        field === 'stage_retries' ? !Number.isSafeInteger(v) || v < 0 : typeof v !== 'string'
      )
    )
      throw new Error(`Invalid ${field}`)
  }
  if (
    config.stage_results !== undefined &&
    (!config.stage_results ||
      typeof config.stage_results !== 'object' ||
      Array.isArray(config.stage_results))
  ) {
    throw new Error('stage_results must be an object')
  }
  return config
}

function outputSchemas() {
  const string = { type: 'string', maxLength: 2000 }
  const text = { ...string, minLength: 1 }
  const explanation = { ...text, maxLength: 600 }
  const object = properties => ({
    type: 'object',
    properties,
    required: Object.keys(properties),
    additionalProperties: false
  })
  const array = items => ({ type: 'array', items })
  const choice = values => ({ type: 'string', enum: values })
  const nullable = schema => ({ anyOf: [schema, { type: 'null' }] })
  const status = values => ({ status: choice([...values, 'blocked']), reason: string })
  const number = { type: 'number', exclusiveMinimum: 0 }
  const version = object({ id: text, tree: text, patch_path: text })
  const workspace = object({
    repo_path: text,
    worktree_dir: text,
    base_commit: text,
    base_tree: text,
    baseline_patch_path: text
  })
  const identity = { id: text, tree: text }
  const location = object({ file: text, symbol: text, line: { type: 'integer', minimum: 1 } })
  const condition = object({
    kind: choice(['semantics', 'input_range', 'isa', 'entry']),
    requirement: {
      ...explanation,
      description:
        '正确实现所需的事实或约束：数学语义、取值/布局范围、指令扩展/VL、接口/分派要求。性能问题归 rationale。'
    }
  })
  const mca = object({
    ...status(['analyzed', 'unavailable']),
    ...identity,
    model: string,
    cost: nullable(number),
    unit: choice(['cycles/work-item']),
    workload: { ...string, description: '一个逻辑工作项的计算与数据规模定义；候选沿用参考定义。' },
    work_items_per_iteration: nullable(number),
    bottleneck: string,
    output_path: nullable(text)
  })
  const candidateMca = object({ ...mca.properties, status: choice(['analyzed', 'unavailable']) })
  return {
    SourceAnalysis: object({
      ...status(['ready']),
      summary: string,
      workspace: nullable(workspace),
      implementations: array(location),
      dispatch_entries: array(location),
      dispatch_note: string,
      input_constraints: array(text),
      output_constraints: array(text),
      test_entries: array(object({ file: text, entry: text })),
      test_gap: string,
      execution_context: text,
      bottleneck_hypothesis: object({
        kind: choice(['compute', 'memory', 'branch', 'mixed', 'unknown']),
        basis: text,
        evidence_paths: array(text)
      })
    }),
    BaselineBuild: object({
      ...status(['ready', 'needs_environment']),
      version: nullable(version),
      build_dir: string,
      context_path: string,
      environment_request: nullable(
        object({ gaps: array(text), command: text, cwd: text, log_path: text })
      )
    }),
    Knowledge: object({
      ...status(['ready']),
      isa_comparison: array(
        object({
          isa: text,
          assessment: {
            ...explanation,
            description:
              '结合环境支持、数据布局、调用入口和已有实现，说明该 ISA 的优化机会及纳入或不纳入 methods 的依据。'
          }
        })
      ),
      methods: {
        ...array(
          object({
            method: explanation,
            rationale: {
              ...explanation,
              description:
                '排序依据、收益假设及供 Implement 用 MCA 评价的指令成本、资源压力、依赖和 spill 等性能问题。'
            },
            conditions: object({ confirmed: array(condition), pending: array(condition) }),
            source: text
          })
        ),
        maxItems: 5
      }
    }),
    Implement: object({
      ...status(['converged', 'budget_exhausted']),
      candidates: {
        ...array(
          object({
            version,
            parent_id: text,
            transform: { ...explanation, description: '本候选相对 parent_id 的代码变换。' },
            build_status: choice(['built', 'failed']),
            log_path: text,
            mca: nullable(candidateMca)
          })
        ),
        maxItems: config.max_candidates,
        description: '按留档顺序返回全部候选，ID 为 c1、c2…；失败候选也计入预算且 mca 为 null。'
      }
    }),
    MCA: mca
  }
}
