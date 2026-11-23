import {METRICS, L1, finite, evidence, aggregate, args, main, readJson, writeJson} from './common.mjs';

export function validate(manifest, parsed, selection = {}) {
  if (manifest.schema_version !== 1 || parsed.schema_version !== 1) {
    throw new Error('Unsupported schema_version');
  }
  if (typeof manifest.run_id !== 'string' || !manifest.run_id.trim()) {
    throw new Error('run_id must be a nonempty string');
  }
  if (!Array.isArray(parsed.rows) || !Array.isArray(parsed.warnings)) {
    throw new Error('Parsed rows and warnings must be arrays');
  }
  if (!selection || typeof selection !== 'object' || Array.isArray(selection)) {
    throw new Error('Selection must be an object');
  }
  for (const key of Object.keys(selection)) {
    if (!METRICS.includes(key)) throw new Error(`Unknown selection metric: ${key}`);
  }
  const ids = new Set();
  for (const row of parsed.rows) {
    if (!row || !METRICS.includes(row.name) || typeof row.id !== 'string' ||
        ids.has(row.id) || !Number.isInteger(row.line) || row.line <= 0) {
      throw new Error('Malformed or duplicate parsed row');
    }
    ids.add(row.id);
  }
  const checks = [];
  const add = (id, status, detail, source = '') => {
    checks.push({id, status, detail, evidence: source});
  };
  const assertion = (id, item, failure = 'limited') => {
    if (!evidence(item) || typeof item.value !== 'boolean') {
      add(id, 'unknown', '需要布尔值及证据定位');
    } else {
      add(id, item.value ? 'pass' : failure, item.reason || id, item.evidence);
    }
  };
  assertion('target_identity', manifest.identity, 'invalid');
  assertion('scope_alignment', manifest.scope);
  assertion('effective_privilege', manifest.privilege);
  assertion('stable_window', manifest.steady);
  assertion('metric_model', manifest.model);
  assertion('window_complete', manifest.window);
  const execution = manifest.execution || {};
  if (!Number.isInteger(execution.tool_exit) || !evidence(execution)) {
    add('tool_exit', 'unknown', '缺少工具退出状态或证据');
  } else {
    add('tool_exit', execution.tool_exit === 0 ? 'pass' : 'invalid',
      `工具退出码 ${execution.tool_exit}`, execution.evidence);
  }
  if (execution.kind === 'batch') {
    if (!Number.isInteger(execution.target_exit) || !evidence(execution)) {
      add('target_exit', 'unknown', '短任务需要最终退出状态及证据');
    } else {
      add('target_exit', execution.target_exit === 0 ? 'pass' : 'invalid',
        `目标退出码 ${execution.target_exit}`, execution.evidence);
    }
    assertion('work_completed', execution.completed, 'invalid');
  } else if (execution.kind === 'service') {
    assertion('service_health', execution.healthy, 'invalid');
    assertion('service_progress', execution.completed, 'invalid');
  } else {
    add('execution_kind', 'unknown', '指定 batch 或 service');
  }
  const requested = manifest.duration?.requested_seconds;
  const actual = manifest.duration?.actual_seconds;
  if (finite(requested) && finite(actual)) {
    if (requested <= 0 || actual <= 0) {
      add('duration', 'invalid', '采集时长必须为正');
    } else if (actual < requested) {
      add('duration', 'limited', `请求 ${requested}s，实际 ${actual}s；须解释范围`);
    } else {
      add('duration', 'pass', `请求 ${requested}s，实际 ${actual}s`);
    }
  } else {
    add('duration', 'unknown', '需要请求与实际采集秒数；不能使用素材时长');
  }
  if (parsed.warnings.length) {
    assertion('warnings_reviewed', manifest.warnings_reviewed);
  }
  const metrics = {};
  for (const name of METRICS) {
    const candidates = parsed.rows.filter(row => row.name === name);
    let chosen;
    if (Object.hasOwn(selection, name)) {
      chosen = candidates.find(row => row.id === selection[name]);
      if (!chosen) throw new Error(`Selection does not identify ${name}`);
    } else if (candidates.length === 1) {
      chosen = candidates[0];
    }
    if (!chosen) {
      add(`field:${name}`, 'unknown', candidates.length ? '多个窗口，需显式选择' : '字段缺失');
      continue;
    }
    metrics[name] = chosen;
    if (chosen.status !== 'parsed' || !finite(chosen.value)) {
      add(`field:${name}`, 'invalid', `不可用字段：${chosen.raw}`, `${parsed.source}:${chosen.line}`);
    } else if (chosen.value < 0) {
      add(`field:${name}`, 'invalid', `负值：${chosen.raw}`, `${parsed.source}:${chosen.line}`);
    } else if (L1.includes(name) && chosen.value > 100) {
      add(`field:${name}`, 'limited', '超过 100%；核实模型与事件，不能裁剪', `${parsed.source}:${chosen.line}`);
    } else {
      add(`field:${name}`, 'pass', chosen.raw, `${parsed.source}:${chosen.line}`);
    }
  }
  const value = name => metrics[name]?.status === 'parsed' ? metrics[name].value : null;
  const cycles = value('Cycles');
  const instructions = value('Instructions');
  const ipc = value('IPC');
  if (cycles === 0) add('cycles_denominator', 'invalid', 'cycles 为零，不能计算 IPC');
  const arithmetic = manifest.arithmetic || {};
  if (arithmetic.same_scope !== true || !evidence(arithmetic)) {
    add('ipc_consistency', 'unknown', '需要确认同窗、同缩放及同聚合口径');
  } else if (![cycles, instructions, ipc].every(finite) || cycles <= 0) {
    add('ipc_consistency', 'unknown', '计数不全，不能复算 IPC');
  } else if (!finite(arithmetic.ipc_tolerance) || arithmetic.ipc_tolerance < 0) {
    add('ipc_consistency', 'unknown', '需要依据显示精度指定绝对容差');
  } else {
    const expected = instructions / cycles;
    add('ipc_consistency', Math.abs(expected - ipc) <= arithmetic.ipc_tolerance ? 'pass' : 'invalid',
      `复算 IPC=${expected}；报告=${ipc}；容差=${arithmetic.ipc_tolerance}`, arithmetic.evidence);
  }
  const partition = manifest.partition || {};
  if (partition.confirmed !== true || !evidence(partition)) {
    add('l1_partition', 'unknown', '未确认本平台/版本 L1 四项为完整分区');
  } else if (!L1.map(value).every(finite)) {
    add('l1_partition', 'unknown', 'L1 字段不全');
  } else if (!finite(partition.tolerance_pp) || partition.tolerance_pp < 0) {
    add('l1_partition', 'unknown', '需要指定有依据的百分点容差');
  } else {
    const total = L1.map(value).reduce((a, b) => a + b, 0);
    add('l1_partition', Math.abs(total - 100) <= partition.tolerance_pp ? 'pass' : 'invalid',
      `一级和=${total}%；容差=${partition.tolerance_pp}pp`, partition.evidence);
  }
  const pmu = manifest.pmu || {};
  const enabled = pmu.time_enabled;
  const running = pmu.time_running;
  if (!finite(enabled) || !finite(running) || !evidence(pmu)) {
    add('pmu_scheduling', 'unknown', '未取得有效时间信息；不伪造运行比例');
  } else if (enabled <= 0 || running <= 0 || running > enabled) {
    add('pmu_scheduling', 'invalid', 'PMU enabled/running 时间关系无效', pmu.evidence);
  } else if (!finite(pmu.minimum_ratio) || pmu.minimum_ratio <= 0 || pmu.minimum_ratio > 1) {
    add('pmu_scheduling', 'unknown', '缺少本次策略的有效运行比例阈值');
  } else {
    add('pmu_scheduling', running / enabled >= pmu.minimum_ratio ? 'pass' : 'limited',
      `运行比例=${running / enabled}；阈值=${pmu.minimum_ratio}`, pmu.evidence);
  }
  const work = manifest.work || {};
  let cyclesPerUnit = null;
  if (work.same_window === true && finite(work.units) && work.units > 0 &&
      evidence(work) && finite(cycles) && cycles > 0) {
    cyclesPerUnit = cycles / work.units;
    add('work_denominator', 'pass', '使用同窗实际完成量', work.evidence);
  } else {
    add('work_denominator', 'limited', '不计算每单位计数：同窗完成量不足');
  }
  const quality = aggregate(checks);
  return {
    schema_version: 1,
    run_id: manifest.run_id,
    source: {path: parsed.source, sha256: parsed.sha256},
    quality_status: quality,
    comparison_candidate: quality === 'pass',
    comparison_eligible: false,
    comparison_reason: '单次审核不证明两组可比；需人工核对配对契约和重复性',
    metrics,
    cycles_per_unit: quality === 'invalid' ? null : cyclesPerUnit,
    checks,
    warnings: parsed.warnings,
    manifest,
  };
}

main(import.meta.url, () => {
  const opt = args(process.argv.slice(2), ['manifest', 'parsed', 'out'], ['selection']);
  const result = validate(readJson(opt.manifest), readJson(opt.parsed),
    opt.selection ? readJson(opt.selection) : {});
  writeJson(opt.out, result);
  // Quality is in JSON, not the process status. Exit 2 denotes invocation errors.
});
