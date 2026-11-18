// 输入契约：验证缺失信息，不补充业务默认值。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export class PgoError extends Error {
  constructor(code, message, status = 'failed') {
    super(message);
    this.code = code;
    this.status = status;
  }
}
export const fail = (code, message, status) => {
  throw new PgoError(code, message, status);
};
export const sha = (value) => crypto.createHash('sha256').update(value).digest('hex');
export const fileHash = (file) => sha(fs.readFileSync(file));
export const readJSON = (file) => JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
export function writeJSON(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(value, null, 2) + '\n');
  fs.renameSync(temp, file);
}
export function within(parent, child) {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel !== '' && !rel.startsWith(`..${path.sep}`) && rel !== '..' && !path.isAbsolute(rel);
}
export function treeHash(root, allowLinks = false) {
  const entries = [];
  function visit(dir, rel = '') {
    for (const item of fs
      .readdirSync(dir, { withFileTypes: true })
      .sort((a, b) => a.name.localeCompare(b.name))) {
      if (item.name === '.git') continue;
      const full = path.join(dir, item.name),
        name = path.posix.join(rel, item.name);
      if (item.isSymbolicLink()) {
        if (!allowLinks)
          fail('source_symlink', `源码/数据目录含符号链接，需先提供独立快照: ${full}`);
        entries.push([name, 'symlink', fs.readlinkSync(full)]);
        continue;
      }
      if (item.isDirectory()) visit(full, name);
      else if (item.isFile()) entries.push([name, fileHash(full)]);
      else fail('special_file', `不支持特殊文件: ${full}`);
    }
  }
  visit(root);
  return sha(JSON.stringify(entries));
}
const text = (x) => typeof x === 'string' && x.trim().length > 0;
const positive = (x) => typeof x === 'number' && Number.isFinite(x) && x > 0;
const integer = (x) => Number.isInteger(x) && x > 0;
const strings = (x) => Array.isArray(x) && x.length > 0 && x.every(text);
const object = (x) => x && typeof x === 'object' && !Array.isArray(x);
export function validate(c) {
  const errors = [];
  const need = (ok, field, message) => {
    if (!ok) errors.push({ code: 'invalid_or_missing_input', field, required_from_user: message });
  };
  if (!object(c))
    return [{ code: 'invalid_contract', field: '$', required_from_user: '提供 JSON 对象' }];
  for (const k of [
    'authorization',
    'approval',
    'environment',
    'tools',
    'build',
    'workload',
    'correctness',
    'measurement',
    'resources',
  ])
    if (c[k] !== undefined && !object(c[k]))
      errors.push({ code: 'invalid_type', field: k, required_from_user: '必须为对象' });
  for (const k of ['datasets', 'training', 'validation', 'artifacts', 'libraries'])
    if (c[k] !== undefined && !Array.isArray(c[k]))
      errors.push({ code: 'invalid_type', field: k, required_from_user: '必须为数组' });
  for (const k of ['targets', 'link_targets', 'configure_args'])
    if (c.build?.[k] !== undefined && !Array.isArray(c.build[k]))
      errors.push({ code: 'invalid_type', field: `build.${k}`, required_from_user: '必须为数组' });
  for (const k of ['datasets', 'training', 'validation', 'libraries'])
    if (Array.isArray(c[k]) && c[k].some((x) => !object(x)))
      errors.push({ code: 'invalid_type', field: k, required_from_user: '元素必须为对象' });
  for (const k of ['training', 'validation'])
    for (const s of Array.isArray(c[k]) ? c[k] : [])
      if (object(s) && s.dataset_ids !== undefined && !Array.isArray(s.dataset_ids))
        errors.push({
          code: 'invalid_type',
          field: `${k}.dataset_ids`,
          required_from_user: '必须为数组',
        });
  if (errors.length) return errors;
  need(c.version === 1, 'version', '版本必须为 1');
  need(
    c.authorization?.explicit_pgo === true && text(c.authorization?.user_request),
    'authorization',
    '提供用户主动要求 PGO 的原话，不得由 Agent 伪造',
  );
  need(
    c.approval?.confirmed === true && text(c.approval?.evidence),
    'approval',
    '提供用户明确实验条件的会话依据',
  );
  need(text(c.source_dir) && path.isAbsolute(c.source_dir), 'source_dir', '指定源码绝对路径');
  need(text(c.run_dir) && path.isAbsolute(c.run_dir), 'run_dir', '指定全新实验绝对路径');
  for (const key of ['source_dir', 'run_dir'])
    if (text(c[key]))
      need(!/[;\r\n]/.test(c[key]), key, 'CMake 路径不支持分号或换行；请指定明确兼容路径');
  if (text(c.source_dir) && text(c.run_dir))
    need(
      !within(c.source_dir, c.run_dir) &&
        !within(c.run_dir, c.source_dir) &&
        path.resolve(c.source_dir) !== path.resolve(c.run_dir),
      'run_dir',
      '源码与实验目录必须互不包含',
    );
  for (const k of ['hostname', 'cpu_scope', 'isolation'])
    need(text(c.environment?.[k]), `environment.${k}`, '明确目标机器、部署 CPU 范围和环境隔离条件');
  need(
    c.environment?.platform === 'linux' && c.environment?.arch === 'arm64',
    'environment',
    '首版只接受 Linux ARM64 本机实验',
  );
  for (const k of ['cc', 'cxx', 'cmake', 'gcov_tool', 'gcov_dump'])
    need(text(c.tools?.[k]) && path.isAbsolute(c.tools[k]), `tools.${k}`, '指定工具绝对路径');
  need(
    c.build?.generator === 'Unix Makefiles',
    'build.generator',
    '首版经过链接证据适配的 generator 为 Unix Makefiles',
  );
  need(
    ['Release', 'RelWithDebInfo'].includes(c.build?.type),
    'build.type',
    '明确 Release 或 RelWithDebInfo',
  );
  need(
    strings(c.build?.targets),
    'build.targets',
    '列出需要编译插桩的 CMake target，包含 driver 中需优化的模板代码',
  );
  need(
    strings(c.build?.link_targets),
    'build.link_targets',
    '列出共享库及最终 driver 链接目标，静态库不要放入',
  );
  need(
    Array.isArray(c.build?.configure_args) && c.build.configure_args.every(text),
    'build.configure_args',
    '明确额外 CMake 参数，无额外参数时提供 []',
  );
  for (const t of [
    ...(Array.isArray(c.build?.targets) ? c.build.targets : []),
    ...(Array.isArray(c.build?.link_targets) ? c.build.link_targets : []),
  ])
    need(
      typeof t === 'string' && /^[A-Za-z0-9_.+-]+$/.test(t),
      'build.targets',
      'target 名只能使用字母数字及 _.+-',
    );
  for (const arg of c.build?.configure_args ?? [])
    if (typeof arg === 'string')
      need(
        arg.startsWith('-D') &&
          !/PGO|PROFILE|CMAKE_(?:C|CXX)_COMPILER|CMAKE_BUILD_TYPE|CMAKE_PROJECT|TOOLCHAIN|EXPORT_COMPILE_COMMANDS|COMPILER_LAUNCHER|^-D.*(?:-fprofile|-fauto-profile|coverage-mismatch|missing-profile)/i.test(
            arg,
          ),
        'build.configure_args',
        `禁止覆盖执行器控制项: ${arg}`,
      );
  need(
    ['single', 'joined_threads'].includes(c.workload?.process_model),
    'workload.process_model',
    '明确单进程单线程或正常 join 的多线程；首版不执行多进程负载',
  );
  need(text(c.workload?.description), 'workload.description', '说明实际调用路径、并发及业务代表性');
  need(
    object(c.workload?.env) &&
      Object.values(c.workload?.env ?? {}).every((x) => typeof x === 'string'),
    'workload.env',
    '明确固定环境变量对象，无则 {}',
  );
  for (const key of Object.keys(c.workload?.env ?? {}))
    need(
      !/^(GCOV_|LD_|DYLD_|PGO_)/.test(key),
      'workload.env',
      `禁止覆盖采集/加载器控制变量 ${key}`,
    );
  need(
    Array.isArray(c.datasets) && c.datasets.length > 0,
    'datasets',
    '明确训练、验证数据；没有外部文件时登记获批的生成参数文件',
  );
  const ids = new Set();
  for (const d of c.datasets ?? []) {
    need(
      text(d.id) && !ids.has(d.id) && /^[a-zA-Z0-9_-]+$/.test(d.id),
      'datasets.id',
      '数据 id 必须唯一',
    );
    ids.add(d.id);
    need(text(d.path) && path.isAbsolute(d.path), 'datasets.path', '数据使用绝对文件路径');
    need(
      text(d.sha256) && /^[a-f0-9]{64}$/.test(d.sha256),
      'datasets.sha256',
      '记录数据文件 SHA256',
    );
    need(
      text(d.description) && text(d.user_evidence),
      'datasets',
      '说明数据来源、内容及用户指定依据',
    );
  }
  const trainIds = new Set();
  for (const group of ['training', 'validation']) {
    const list = c[group];
    need(Array.isArray(list) && list.length > 0, group, '至少一个明确场景');
    const names = new Set();
    for (const s of Array.isArray(list) ? list : []) {
      need(
        text(s.id) && /^[a-zA-Z0-9_-]+$/.test(s.id) && !names.has(s.id),
        `${group}.id`,
        '场景 id 唯一且仅字母数字 _-',
      );
      names.add(s.id);
      need(strings(s.argv), `${group}.argv`, '明确 argv 数组，禁止隐式 shell');
      if (strings(s.argv))
        need(
          path.isAbsolute(s.argv[0]) || /^\{(?:build|source)\}\//.test(s.argv[0]),
          `${group}.argv`,
          '可执行程序使用绝对路径或 {build}/{source} 路径，不依赖 PATH',
        );
      need(
        strings(s.dataset_ids) && s.dataset_ids.every((id) => ids.has(id)),
        `${group}.dataset_ids`,
        '声明场景使用的数据 id',
      );
      for (const id of s.dataset_ids ?? []) if (group === 'training') trainIds.add(id);
      need(text(s.description), `${group}.description`, '明确模式、规模、调用比例及参数');
      if (group === 'training')
        need(
          integer(s.runs),
          `${group}.runs`,
          '明确训练运行次数；次数体现已批准工作量，不自动加权',
        );
      else
        need(
          positive(s.weight) && Number.isFinite(s.max_regression_pct) && s.max_regression_pct >= 0,
          `${group}.weight`,
          '明确验证聚合权重及允许退化百分比',
        );
    }
  }
  for (const s of c.validation ?? [])
    for (const id of s.dataset_ids ?? []) {
      const d = (c.datasets ?? []).find((x) => x.id === id);
      need(
        !trainIds.has(id) &&
          !Array.from(trainIds).some(
            (t) => (c.datasets ?? []).find((x) => x.id === t)?.sha256 === d?.sha256,
          ),
        'validation.dataset_ids',
        '首版完整验收要求训练与验证文件内容独立',
      );
    }
  need(
    strings(c.correctness?.argv) && text(c.correctness?.description),
    'correctness',
    '指定自校验程序和判定语义；必须输出 correct:true',
  );
  if (strings(c.correctness?.argv))
    need(
      path.isAbsolute(c.correctness.argv[0]) ||
        /^\{(?:build|source)\}\//.test(c.correctness.argv[0]),
      'correctness.argv',
      '可执行程序必须为明确路径',
    );
  need(
    ['higher', 'lower'].includes(c.measurement?.direction) && text(c.measurement?.metric),
    'measurement',
    '明确指标名及方向',
  );
  need(
    integer(c.measurement?.pairs) && c.measurement.pairs >= 6,
    'measurement.pairs',
    '至少 6 个独立 B/P 配对（需用户批准）',
  );
  need(
    Number.isInteger(c.measurement?.warmups) && c.measurement.warmups >= 0,
    'measurement.warmups',
    '明确预热次数，允许 0',
  );
  need(positive(c.measurement?.max_cv_pct), 'measurement.max_cv_pct', '明确测量变异系数上限');
  need(positive(c.measurement?.min_gain_pct), 'measurement.min_gain_pct', '明确最小收益百分比');
  need(
    integer(c.measurement?.bootstrap_samples) && c.measurement.bootstrap_samples >= 1000,
    'measurement.bootstrap_samples',
    '明确 bootstrap 次数，至少 1000',
  );
  need(
    Number.isInteger(c.measurement?.seed) &&
      c.measurement.seed >= 0 &&
      c.measurement.seed <= 4294967295,
    'measurement.seed',
    '明确可重现抽样种子',
  );
  need(
    c.measurement?.confidence === 0.95,
    'measurement.confidence',
    '首版固定算法支持 95% 区间，需在计划中明确',
  );
  for (const key of ['jobs', 'timeout_seconds', 'total_seconds', 'min_free_bytes'])
    need(integer(c.resources?.[key]), `resources.${key}`, '指定正整数资源预算');
  need(strings(c.artifacts), 'artifacts', '指定 build 内要核验的 driver 与库相对路径');
  for (const p of c.artifacts ?? [])
    need(
      text(p) && !path.isAbsolute(p) && !p.split(/[\\/]/).includes('..'),
      'artifacts',
      '产物不得逃逸 build',
    );
  need(
    Array.isArray(c.libraries) && c.libraries.length > 0,
    'libraries',
    '明确待优化库的 target、文件及类型',
  );
  for (const l of c.libraries ?? []) {
    need(
      ['static', 'shared'].includes(l.kind) &&
        c.build?.targets?.includes(l.target) &&
        c.artifacts?.includes(l.path),
      'libraries',
      '库 target 必须被插桩，path 必须属于 artifacts',
    );
    if (l.kind === 'static')
      need(
        text(l.link_target) && c.build?.link_targets?.includes(l.link_target),
        'libraries.link_target',
        '静态库声明最终链接目标',
      );
  }
  return errors;
}
export function assertContract(c) {
  const errors = validate(c);
  if (errors.length) {
    const e = new PgoError('input_gate', '必要输入缺失或无效', 'awaiting_input');
    e.blockers = errors;
    throw e;
  }
}
