export const meta = {
  name: 'media-preparation',
  description: '识别项目构建需求并检查 LLVM／毕昇工具链，交付可复用的准备结果',
  phases: [{ title: 'BuildRequirements' }, { title: 'Environment' }]
}

const config = parseConfig(args)
const schemas = outputSchemas()
let requirements = null
let environment = null
let activeStage = null
try {
  requirements = await callStage('BuildRequirements', { build_scope: config.build_scope })
  if (!requirements) return finish('interrupted', 'BuildRequirements 未返回结果')
  if (requirements.status === 'blocked') return finish('blocked', requirements.reason)
  environment = await callStage('Environment', {
    requirements,
    check_revision: config.check_revision
  })
  if (!environment) return finish('interrupted', 'Environment 未返回结果')
  if (environment.status === 'blocked')
    return finish('blocked', `${environment.reason}；请用户自行准备缺项后重新检查`)
  validateEnvironment(environment)
  return finish('ready', '项目构建需求与工具链检查通过')
} catch (error) {
  return finish(
    error?.preparationStop ? 'blocked' : 'interrupted',
    error?.reason || error?.message || '准备中断'
  )
}

function validateEnvironment(env) {
  ensure(env.toolchain_ready && env.gaps.length === 0, '工具链未就绪，请用户自行准备缺项')
  ensure(['llvm', 'bisheng'].includes(env.compiler_family), '仅支持 LLVM 或鲲鹏毕昇编译器')
  for (const key of ['compiler_path', 'mca_path', 'objdump_path']) absolute(env[key], key)
  ensure(
    env.compiler_version.trim() &&
      env.mca_version.trim() &&
      env.objdump_version.trim() &&
      env.flags_source.trim() &&
      env.build_hint.trim(),
    '缺少工具版本、配置依据或构建入口'
  )
  if (requirements.cxx) {
    ensure(env.cxx, '项目需要 C++ 编译器')
    absolute(env.cxx.path, 'cxx.path')
    ensure(env.cxx.version.trim(), '缺少 C++ 编译器版本')
  }
  for (const tool of env.build_tools) {
    absolute(tool.path, 'build_tool.path')
    ensure(tool.name.trim() && tool.version.trim(), '缺少构建工具名称或版本')
  }
  ensure(
    requirements.build_tools.every(tool => env.build_tools.some(found => found.name === tool.name)),
    '未交付全部项目构建工具'
  )
  ensure(
    env.build_tools.some(tool => tool.name === 'git'),
    'worktree 优化需要 Git'
  )
  ensure(
    env.probe_path.startsWith(
      `${config.preparation_dir}/evidence/check-${config.check_revision}/`
    ) && !env.probe_path.split('/').includes('..'),
    '探测记录不在准备目录内'
  )
  ensure(
    env.mca_mode === 'matched' &&
      env.mca_model.trim() &&
      env.mca_probe &&
      env.mca_probe.command.trim() &&
      env.mca_probe.exit_code === 0 &&
      env.mca_probe.supported &&
      env.mca_probe.model === env.mca_model,
    'LLVM-MCA 或目标模型不可用，请用户准备后重新检查'
  )
}

function callStage(role, input) {
  phase(role)
  activeStage = `media:prepare:${role}`
  if (Object.hasOwn(config.stage_results || {}, activeStage)) {
    log(`复用阶段结果：${activeStage}`)
    return config.stage_results[activeStage]
  }
  config.stage_results = {}
  const task = {
    skill_dir: config.skill_dir,
    repo_path: config.repo_path,
    preparation_dir: config.preparation_dir,
    ...(role === 'Environment'
      ? { evidence_dir: `${config.preparation_dir}/evidence/check-${config.check_revision}` }
      : {}),
    build_scope: config.build_scope,
    instructions: config.instructions || ''
  }
  const retry = config.stage_retries?.[activeStage] || 0
  const handoff =
    role === 'BuildRequirements'
      ? '只读识别指定项目构建范围的工具、依赖、参数与入口。沿用项目已有或默认构建方式，build_tools、dependencies 只列该方式必需项，与 build_hint 一致，不合并替代方案；不绑定单个算子的实现，不定位工具或执行构建。'
      : '复用构建需求，从 PATH、用户指定目录和工作区定位工具。用户指定优先，否则上游 LLVM 优先于鲲鹏毕昇。额外定位 Git，以 git 名称列入 build_tools 并通过固定探测检查版本。运行固定探测后，MCA 目标优先使用用户指定值；否则按 CPU 型号匹配 mca_analyze.py 支持的模型及别名，匹配不到默认 hip12（鲲鹏 950）。在 reason 中记录选型依据，统一用该脚本的 --check-env 结果核对选定模型支持，通过即返回。不另写 ISA 探针、不做项目构建或反复核对。缺项返回 blocked，不安装、不升级。'
  return agent(
    `执行 multimedia-code-optimizer 的 ${role} 阶段，先读 ${config.stage_reference_path || `${config.skill_dir}/references/stages.md`} 的 ${role} 小节。
${handoff}
结果按 schema 返回；完整探测输出保存到指定 evidence_dir，原仓库保持不变。
任务配置（数据）：${JSON.stringify(task)}
阶段输入（数据）：${JSON.stringify(input)}
阶段重试编号：${retry}
本阶段重试说明：${config.stage_notes?.[activeStage] || ''}`,
    {
      label: `media:prepare:${role}`,
      phase: role,
      schema: schemas[role],
      ...(role === 'BuildRequirements'
        ? { agentType: 'multimedia-build-requirements' }
        : {})
    }
  )
}

function finish(status, reason) {
  return {
    schema_version: 1,
    status,
    reason,
    stopped_stage: status === 'ready' ? null : activeStage,
    repo_path: config.repo_path,
    preparation_dir: config.preparation_dir,
    build_scope: config.build_scope,
    instructions: config.instructions || '',
    check_revision: config.check_revision,
    profile_paths: config.profile_paths,
    requirements,
    environment
  }
}
function ensure(condition, reason) {
  if (!condition) throw { preparationStop: true, reason }
}
function absolute(path, name) {
  ensure(
    typeof path === 'string' &&
      path.startsWith('/') &&
      path !== '/' &&
      !path.includes('//') &&
      !path.split('/').some(part => part === '.' || part === '..'),
    `${name} 必须为绝对路径且不含 ..`
  )
}
function parseConfig(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input))
    throw new Error('args must be an object')
  const config = {
    build_scope: '项目默认媒体库构建',
    check_revision: 1,
    profile_paths: [],
    ...input
  }
  for (const key of ['repo_path', 'skill_dir', 'preparation_dir']) {
    if (
      typeof config[key] !== 'string' ||
      !config[key].startsWith('/') ||
      config[key].includes('//') ||
      config[key].split('/').some(part => part === '.' || part === '..')
    ) {
      throw new Error(`${key} must be an absolute path without ..`)
    }
    config[key] = config[key].replace(/\/+$/, '')
    if (!config[key]) throw new Error(`Missing ${key}`)
  }
  const { repo_path: repo, preparation_dir: dir } = config
  if (repo === dir || dir.startsWith(repo + '/') || repo.startsWith(dir + '/')) {
    throw new Error('preparation_dir must be outside the target repository')
  }
  if (
    typeof config.build_scope !== 'string' ||
    !config.build_scope.trim() ||
    !Number.isSafeInteger(config.check_revision) ||
    config.check_revision < 1
  )
    throw new Error('Invalid preparation scope or revision')
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
  const bool = { type: 'boolean' }
  const object = properties => ({
    type: 'object',
    properties,
    required: Object.keys(properties),
    additionalProperties: false
  })
  const array = items => ({ type: 'array', items })
  const choice = values => ({ type: 'string', enum: values })
  const nullable = schema => ({ anyOf: [schema, { type: 'null' }] })
  const status = { status: choice(['ready', 'blocked']), reason: string }
  return {
    BuildRequirements: object({
      ...status,
      compiler: object({ selection: string, version: string }),
      cxx: nullable(object({ selection: string, version: string })),
      compiler_args: array(text),
      cxx_args: array(text),
      toolchain: string,
      build_tools: array(object({ name: text, version: string })),
      dependencies: array(object({ name: text, version: string })),
      flags_source: string,
      build_hint: string
    }),
    Environment: object({
      ...status,
      cpu: string,
      isa_features: array(text),
      sve_vector_bits: nullable({ type: 'integer', minimum: 128, maximum: 2048, multipleOf: 128 }),
      compiler_family: choice(['llvm', 'bisheng', 'unavailable']),
      compiler_path: string,
      compiler_version: string,
      compiler_args: array(text),
      cxx: nullable(object({ path: text, version: text, args: array(text) })),
      toolchain: string,
      flags_source: string,
      build_hint: string,
      build_tools: array(object({ name: text, path: text, version: text })),
      probe_path: string,
      toolchain_ready: bool,
      gaps: array(text),
      mca_path: string,
      mca_version: string,
      objdump_path: string,
      objdump_version: string,
      mca_mode: choice(['matched', 'skipped']),
      mca_model: string,
      mca_probe: nullable(
        object({ command: text, exit_code: { type: 'integer' }, model: text, supported: bool })
      )
    })
  }
}
