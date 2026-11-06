export const meta = {
  name: 'media-validation',
  description: '准备片段测试、独立验证，验证通过后写回选中补丁并清理任务 worktree',
  phases: [{ title: 'ValidationSetup' }, { title: 'Correctness' }, { title: 'Writeback' }]
}

let config = {}
const schemas = outputSchemas()
const state = {
  active_stage: 'media:validation:setup',
  context_path: null,
  validation_setup: null,
  correctness: { status: 'pending_validation', reason: '验证尚未完成' },
  writeback: {
    status: 'skipped',
    reason: '尚未满足写回条件',
    applied: false,
    cleanup: 'retained',
    changed_files: []
  }
}
return await validate()

async function validate() {
  try {
    config = parseConfig(args)
    const candidate = config.optimization.selected
    const baseline = config.optimization.baseline
    if (!candidate) stop('blocked', '阶段二未选出交付版本，等待有效的 optimization 结果')
    schemas.Correctness.properties.id = {
      ...schemas.Correctness.properties.id,
      enum: [candidate.id]
    }
    if (candidate.id === baseline.id) {
      state.correctness = { status: 'not_started', reason: '选中原版，跳过重复验证' }
      state.writeback.reason = '选中基线，不写回，保留任务 worktree'
      return finish('completed', '保留阶段二选定的原版')
    }
    const setup = await callStage('ValidationSetup', 'media:validation:setup', {
      optimization: config.optimization,
      optimization_path: config.optimization_path,
      baseline,
      candidate,
      workspace: config.optimization.workspace,
      source_context_path: config.optimization.context_path,
      context_path: `${config.validation_dir}/context.json`,
      baseline_build_dir: `${config.validation_dir}/baseline/build`,
      candidate_build_dir: `${config.validation_dir}/candidate/build`,
      harness_dir: `${config.validation_dir}/harness`,
      evidence_dir: `${config.validation_dir}/evidence/setup`
    })
    state.validation_setup = setup
    ensure(['ready', 'blocked'].includes(setup.status), 'ValidationSetup 状态无效')
    if (setup.status === 'blocked') {
      ensure(nonempty(setup.reason), '验证准备阻塞缺少原因')
      stop('blocked', setup.reason)
    }
    checkIdentities(setup.versions)
    ensure(
      setup.context_path === `${config.validation_dir}/context.json`,
      '必须写入独立的验证 context.json'
    )
    ensure(Array.isArray(setup.smoke) && setup.smoke.length > 0, '缺少入口冒烟记录')
    for (const execution of setup.smoke) {
      ensure(execution.exit_code === 0, '验证入口冒烟未通过')
      checkAbsolute(execution.cwd, 'smoke.cwd')
      checkEvidence(execution.log_path)
    }
    state.context_path = setup.context_path
    ensure(
      object(setup.correctness) &&
        nonempty(setup.correctness.scope) &&
        Array.isArray(setup.correctness.test_paths) &&
        setup.correctness.test_paths.length > 0 &&
        Array.isArray(setup.correctness.commands) &&
        setup.correctness.commands.some(command => command.kind === 'validation'),
      '缺少片段测试、验证范围或执行配置'
    )
    for (const path of setup.correctness.test_paths) {
      checkAbsolute(path, 'test_path')
      ensure(path.startsWith(`${config.validation_dir}/`), '测试文件必须位于验证目录内')
    }
    const check = await callStage('Correctness', `media:${candidate.id}:final:check`, {
      baseline,
      candidate,
      correctness: setup.correctness,
      evidence_dir: `${config.validation_dir}/evidence/correctness`
    })
    ensure(
      ['passed', 'failed', 'blocked', 'pending_validation'].includes(check.status),
      '正确性状态无效'
    )
    checkIdentity(check, candidate)
    checkIdentities(check.versions)
    checkCorrectnessEvidence(check)
    if (check.status === 'blocked' || check.status === 'pending_validation') {
      ensure(nonempty(check.reason), '待验证结果缺少原因')
      state.correctness = { ...check, status: 'pending_validation' }
      stop('blocked', check.reason)
    }
    validateCorrectness(check, setup.correctness.commands)
    state.correctness = check
    if (check.status !== 'passed') {
      state.writeback.reason = '独立验证未通过，不写回，保留任务 worktree'
      return finish('completed', check.reason || '所选版本正确性失败，保留失败证据')
    }
    if (!config.writeback) {
      state.writeback.reason = '用户要求不写回，保留补丁、证据与任务 worktree'
      return finish('completed', '独立验证通过，按要求不写回')
    }
    state.writeback = {
      status: 'pending',
      reason: '写回进行中，以回执核对实际状态',
      applied: null,
      cleanup: 'unknown',
      changed_files: []
    }
    const delivery = await callStage('Writeback', 'media:writeback', {
      repo_path: config.optimization.repo_path,
      workspace: config.optimization.workspace,
      workspace_path: `${config.optimization.run_dir}/workspace.json`,
      baseline,
      candidate,
      correctness: check,
      receipt_path: `${config.validation_dir}/writeback.json`,
      evidence_dir: `${config.validation_dir}/evidence/writeback`
    })
    state.writeback = delivery
    checkIdentity(delivery, candidate)
    ensure(
      ['completed', 'blocked'].includes(delivery.status) &&
        typeof delivery.applied === 'boolean' &&
        ['removed', 'retained'].includes(delivery.cleanup) &&
        Array.isArray(delivery.changed_files),
      '写回结果无效'
    )
    checkEvidence(delivery.log_path)
    ensure(
      delivery.changed_files.every(
        path =>
          nonempty(path) &&
          !path.startsWith('/') &&
          !path.includes('\\\\') &&
          !path.split('/').some(part => ['.', '..', '.git', ''].includes(part))
      ),
      '写回文件必须为仓库相对路径'
    )
    if (delivery.status === 'blocked') {
      ensure(
        nonempty(delivery.reason) && delivery.cleanup === 'retained',
        '写回阻塞应保留 worktree 并说明原因'
      )
      stop('blocked', delivery.reason)
    }
    ensure(
      delivery.applied && delivery.cleanup === 'removed' && delivery.changed_files.length > 0,
      '写回完成必须已应用补丁并清理任务 worktree'
    )
    return finish('completed', '独立验证通过，选中补丁已写回，任务 worktree 已清理，未提交')
  } catch (error) {
    const status = error?.workflowStop ? error.status : 'interrupted'
    const reason = error?.reason || error?.message || '验证中断'
    if (state.correctness.status === 'pending_validation')
      state.correctness = { ...state.correctness, reason }
    if (state.active_stage === 'media:writeback')
      state.writeback = { ...state.writeback, status, reason }
    return finish(status, reason)
  }
}

function checkCorrectnessEvidence(check) {
  ensure(Array.isArray(check.executions), '正确性执行记录无效')
  for (const execution of check.executions) {
    checkEvidence(execution.log_path)
    checkAbsolute(execution.cwd, 'execution.cwd')
    ensure(['validation', 'support', 'negative_control'].includes(execution.kind), '执行种类无效')
  }
  if (check.failure_input_path !== null) checkEvidence(check.failure_input_path)
}

function validateCorrectness(check, commands) {
  const countsValid =
    Number.isSafeInteger(check.passed_cases) &&
    check.passed_cases >= 0 &&
    Number.isSafeInteger(check.failed_cases) &&
    check.failed_cases >= 0
  const commandsComplete =
    check.executions.length > 0 &&
    check.executions.every(
      execution =>
        nonempty(execution.command) &&
        Number.isInteger(execution.exit_code) &&
        Number.isInteger(execution.expected_exit_code)
    )
  const errorValid = Number.isFinite(check.max_abs_error) && check.max_abs_error >= 0
  ensure(
    countsValid &&
      commandsComplete &&
      nonempty(check.comparison_rule) &&
      ['exact', 'floating', 'mixed'].includes(check.comparison) &&
      (check.max_abs_error === null || errorValid),
    '正确性缺少命令、预期退出码、用例统计或有效误差'
  )
  const primary = check.executions.filter(execution => execution.kind === 'validation')
  ensure(primary.length > 0, '缺少候选正确性验证命令')
  if (check.status === 'passed') {
    ensure(
      check.executions.length === commands.length &&
        commands.every((command, index) =>
          ['command', 'cwd', 'kind', 'expected_exit_code'].every(
            field => check.executions[index][field] === command[field]
          )
        ),
      '验证通过必须按计划完整执行命令，工作目录、顺序、类型及预期退出码应一致'
    )
    ensure(
      check.passed_cases > 0 &&
        check.failed_cases === 0 &&
        check.failure_input_path === null &&
        primary.every(
          execution => execution.exit_code === 0 && execution.expected_exit_code === 0
        ) &&
        check.executions.every(
          execution =>
            execution.exit_code === execution.expected_exit_code &&
            (execution.kind === 'negative_control' || execution.exit_code === 0)
        ) &&
        (check.comparison === 'exact'
          ? check.max_abs_error === null || check.max_abs_error === 0
          : errorValid),
      '通过声明与用例统计、退出码或误差不一致'
    )
  } else
    ensure(
      check.failed_cases > 0 && check.failure_input_path !== null,
      '失败结果缺少用例计数或复现输入'
    )
}

async function callStage(role, label, input) {
  phase(role)
  state.active_stage = label
  if (Object.hasOwn(config.stage_results || {}, label)) {
    log(`复用阶段结果：${label}`)
    return config.stage_results[label]
  }
  config.stage_results = {}
  const task = {
    skill_dir: config.skill_dir,
    validation_dir: config.validation_dir,
    operator: config.optimization.operator,
    instructions: config.instructions || config.optimization.instructions || ''
  }
  const handoff = {
    ValidationSetup:
      '读取优化结果与 context，以 id/tree/patch_path 核对版本。在唯一任务 worktree 中依次还原并构建基线与选中版，核对选中补丁从基线还原得到相同 tree；两版独立测试入口和运行资源放在 validation_dir，切换源码不影响另一入口。testbench 直接调用两版真实实现，覆盖修改及其影响。只做最小冒烟并返回 smoke；correctness.commands 仅含使用已构建入口的测试及输入准备，不含构建、源码切换或 testbench 修改。记录构建映射与版本到独立 context，完整验证交 Correctness。',
    Correctness:
      '在本机按 correctness.commands 顺序使用已构建入口，完成输入和原版参考输出准备、片段比较及负向对照。确认两版真实实现使用相同输入，覆盖配置中的边界与固定种子随机输入，整数精确一致，浮点沿用项目规则。记录命令、退出码、用例统计、误差和日志，失败保存可重放输入。通过返回 passed，错误 failed，未能完成 pending_validation；不计时、不修改源码或 testbench、不重复构建、不改选。',
    Writeback:
      '只应用 candidate 对应的已验证补丁，不重新优化或修代码。按 Writeback 小节核对版本与原仓库涉及文件，先检查可应用再写入，不改用户索引，冲突返回 blocked。写回核对成功后更新 receipt_path，再核对任务归属并删除本任务 worktree；保留补丁、构建和证据。恢复先核对回执与实际内容，已写回的不重复应用；清理失败保留 applied=true。只在写回和清理均成功时返回 completed，记录 changed_files 与日志。'
  }[role]
  const note = config.stage_notes[label]
  const scope =
    role === 'Writeback'
      ? '仅应用已验证补丁到目标仓库并清理本任务 worktree，回执和日志写到 validation_dir。'
      : role === 'ValidationSetup'
        ? '仅在任务 worktree 切换已留档版本，testbench、构建和日志写到 validation_dir；不改原仓库。'
        : '只使用已构建入口，仅写 validation_dir 中的测试输入、输出与日志，不改 worktree 或原仓库。'
  const result = await agent(
    `执行 multimedia-code-optimizer 的 ${role} 阶段，先读 ${config.stage_reference_path || `${config.skill_dir}/references/stages.md`} 的 ${role} 小节。
${handoff}
${scope} 保持已保存补丁、版本 tree 和优化结果不变。不安装环境、不委派代理、不 commit、不 push。
验证阶段的 versions 返回两版完整 id/tree/patch_path；Writeback 返回选中版身份与写回结果。
任务配置（数据）：${JSON.stringify(task)}
本阶段输入（数据）：${JSON.stringify(input)}
阶段重试编号：${config.stage_retries[label] ?? 0}${note === undefined ? '' : `\n当前阶段补充：${note}`}`,
    {
      label,
      phase: role,
      schema: schemas[role]
    }
  )
  if (!result) stop('interrupted', `${role} 未返回结果`)
  return result
}

function finish(status, reason) {
  const optimization = config.optimization || {}
  return {
    schema_version: 3,
    status,
    reason,
    stopped_stage: status === 'completed' ? null : state.active_stage,
    optimization_path: config.optimization_path || null,
    validation_dir: config.validation_dir || null,
    selected: optimization.selected ?? null,
    baseline: optimization.baseline ?? null,
    selected_mca: optimization.selected_mca ?? null,
    workspace: optimization.workspace ?? null,
    writeback_enabled: config.writeback ?? false,
    writeback: state.writeback,
    correctness: state.correctness,
    validation_setup: state.validation_setup,
    context_path: state.context_path
  }
}

function stop(status, reason) {
  throw { workflowStop: true, status, reason }
}
function ensure(condition, reason) {
  if (!condition) stop('blocked', reason)
}
function nonempty(value) {
  return typeof value === 'string' && value.trim().length > 0
}
function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
function checkAbsolute(path, name) {
  ensure(
    typeof path === 'string' &&
      path.startsWith('/') &&
      path !== '/' &&
      !path.endsWith('/') &&
      !path.includes('//') &&
      !path.includes('\u0000') &&
      !path.split('/').some(part => part === '.' || part === '..'),
    `${name} 必须是规范绝对路径，不能含 .、..、// 或尾随 /`
  )
}
function overlaps(a, b) {
  return a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`)
}
function checkEvidence(path) {
  checkAbsolute(path, '证据路径')
  ensure(
    path.startsWith(`${config.validation_dir}/evidence/`),
    '证据必须保存在 validation_dir/evidence 内'
  )
}
function gitObject(value) {
  return typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value)
}
function checkVersion(version, name) {
  ensure(
    object(version) && nonempty(version.id) && gitObject(version.tree),
    `${name} 缺少版本 ID 或 tree`
  )
  checkAbsolute(version.patch_path, `${name}.patch_path`)
}
function checkIdentity(response, version) {
  ensure(
    response &&
      response.id === version.id &&
      response.tree === version.tree &&
      response.patch_path === version.patch_path,
    '结果对应的版本 ID、tree 或补丁与输入不符'
  )
}
function checkIdentities(versions) {
  ensure(
    Array.isArray(versions) && versions.length === 2,
    '必须核对 baseline 和 candidate 两版身份'
  )
  const expected = [config.optimization.baseline, config.optimization.selected]
  const seen = new Set()
  for (const identity of versions) {
    const version = expected.find(item => item.id === identity?.id)
    ensure(version && !seen.has(identity.id), '版本身份缺失、重复或不匹配')
    checkIdentity(identity, version)
    seen.add(identity.id)
  }
}
function parseConfig(input) {
  ensure(object(input), 'args 必须为对象')
  config = { ...input }
  for (const key of ['skill_dir', 'validation_dir', 'optimization_path'])
    checkAbsolute(config[key], key)
  const optimization = config.optimization
  ensure(
    object(optimization) && optimization.schema_version === 5,
    '需要 schema_version=5 的 worktree 优化结果'
  )
  ensure(
    ['converged', 'budget_exhausted'].includes(optimization.status),
    '仅正常结束的优化结果可进入验证与写回'
  )
  config.writeback = config.writeback ?? optimization.writeback ?? true
  ensure(typeof config.writeback === 'boolean', 'writeback 必须为布尔值')
  for (const key of ['repo_path', 'run_dir', 'context_path'])
    checkAbsolute(optimization[key], `optimization.${key}`)
  ensure(
    nonempty(optimization.operator) && nonempty(optimization.status),
    '阶段二结果缺少 operator 或 status'
  )
  ensure(
    object(optimization.environment) && object(optimization.source),
    '阶段二结果缺少环境或源码分析'
  )
  for (const field of ['stage_retries', 'stage_notes']) {
    config[field] = config[field] ?? {}
    ensure(object(config[field]), `${field} 必须为对象`)
    for (const value of Object.values(config[field])) {
      ensure(
        field === 'stage_retries'
          ? Number.isSafeInteger(value) && value >= 0
          : typeof value === 'string',
        `${field} 值必须为${field === 'stage_retries' ? '非负整数' : '字符串'}`
      )
    }
  }
  const workspace = optimization.workspace
  ensure(
    object(workspace) &&
      workspace.repo_path === optimization.repo_path &&
      typeof workspace.worktree_dir === 'string' &&
      workspace.worktree_dir.startsWith(`${optimization.run_dir}/`) &&
      /^worktree-[A-Za-z0-9]{8}$/.test(
        workspace.worktree_dir.slice(optimization.run_dir.length + 1)
      ) &&
      workspace.baseline_patch_path === `${optimization.run_dir}/baseline.patch` &&
      gitObject(workspace.base_commit) &&
      gitObject(workspace.base_tree),
    'worktree 或任务基线身份无效'
  )
  ensure(!overlaps(optimization.run_dir, optimization.repo_path), '任务目录必须位于原仓库外')
  checkVersion(optimization.baseline, 'optimization.baseline')
  ensure(
    optimization.baseline.id === 'baseline' &&
      optimization.baseline.tree === workspace.base_tree &&
      optimization.baseline.patch_path === workspace.baseline_patch_path,
    '基线与 workspace 不一致'
  )
  ensure(optimization.selected === null || object(optimization.selected), 'selected 无效')
  if (optimization.selected) {
    checkVersion(optimization.selected, 'optimization.selected')
    if (optimization.selected.id === 'baseline')
      checkIdentity(optimization.selected, optimization.baseline)
    else {
      ensure(
        /^c[1-9][0-9]*$/.test(optimization.selected.id) &&
          optimization.selected.patch_path ===
            `${optimization.run_dir}/candidates/${optimization.selected.id}.patch`,
        '选中候选补丁路径无效'
      )
      const candidate = optimization.candidates?.find(
        item => item.version?.id === optimization.selected.id
      )
      ensure(
        candidate?.build_status === 'built' && candidate.mca?.status === 'analyzed',
        '选中候选没有有效构建与 MCA'
      )
      checkIdentity(candidate.version, optimization.selected)
    }
  }
  const protectedPaths = [
    optimization.repo_path,
    optimization.run_dir,
    optimization.context_path,
    config.optimization_path,
    config.skill_dir
  ]
  for (const path of protectedPaths)
    ensure(
      !overlaps(config.validation_dir, path),
      'validation_dir 必须与原仓库、任务目录和输入文件独立'
    )
  ensure(
    config.stage_results === undefined || object(config.stage_results),
    'stage_results 必须为对象'
  )
  return config
}

function outputSchemas() {
  const string = { type: 'string', maxLength: 4000 }
  const text = { ...string, minLength: 1 }
  const integer = { type: 'integer', minimum: 0 }
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
  const identity = { id: text, tree: text, patch_path: text }
  const versions = array(object(identity))
  const command = { command: text, cwd: text }
  const testCommand = {
    ...command,
    expected_exit_code: { type: 'integer' },
    kind: {
      ...choice(['validation', 'support', 'negative_control']),
      description:
        'validation 为两版输出比较，support 为输入/参考准备及比较器自检，negative_control 为预期失败的对照。均使用已构建入口。'
    }
  }
  const comparison = choice(['exact', 'floating', 'mixed'])
  return {
    ValidationSetup: object({
      ...status(['ready']),
      context_path: text,
      versions,
      smoke: array(
        object({
          ...command,
          inputs: { ...text, description: '确认入口可调用及输出可读取的最小输入范围。' },
          exit_code: { type: 'integer' },
          log_path: text
        })
      ),
      correctness: nullable(
        object({
          scope: {
            ...text,
            description: '选中版相对原版的修改片段及其布局、分派、批处理等受影响范围。'
          },
          test_paths: {
            ...array(text),
            minItems: 1,
            description: '验证目录内片段测试源码或脚本的绝对路径。'
          },
          commands: {
            ...array(object(testCommand)),
            minItems: 1,
            description:
              '供 Correctness 顺序执行的片段测试命令，包含所需的输入/原版参考准备与负向对照。'
          },
          inputs: text,
          comparison,
          comparison_rule: text
        })
      )
    }),
    Correctness: object({
      ...status(['passed', 'failed', 'pending_validation']),
      ...identity,
      versions,
      executions: array(
        object({ ...testCommand, exit_code: nullable({ type: 'integer' }), log_path: text })
      ),
      passed_cases: nullable(integer),
      failed_cases: nullable(integer),
      comparison,
      comparison_rule: string,
      max_abs_error: nullable({ type: 'number', minimum: 0 }),
      failure_input_path: nullable(text)
    }),
    Writeback: object({
      ...status(['completed']),
      ...identity,
      applied: { type: 'boolean' },
      cleanup: choice(['removed', 'retained']),
      changed_files: array(text),
      log_path: text
    })
  }
}
