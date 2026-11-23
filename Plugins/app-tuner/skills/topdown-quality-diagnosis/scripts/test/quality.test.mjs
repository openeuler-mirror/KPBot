import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {inspect, parseValue} from '../inspect_artifacts.mjs';
import {validate} from '../validate_metrics.mjs';
import {report} from '../build_quality_report.mjs';

const raw = [
  'Cycles 1,000',
  'Instructions 2,000',
  'IPC 2.00',
  'Bad Speculation 13.03',
  'Frontend Bound 11.04',
  'Retiring 38.62',
  'Backend Bound 37.30',
].join('\n');

function manifest() {
  const yes = () => ({value: true, evidence: 'synthetic-fixture:1'});
  return {
    schema_version: 1,
    run_id: 'synthetic-test-only',
    identity: yes(),
    scope: yes(),
    privilege: yes(),
    steady: yes(),
    model: yes(),
    window: yes(),
    execution: {
      kind: 'batch',
      tool_exit: 0,
      target_exit: 0,
      evidence: 'synthetic-status:1',
      completed: yes(),
    },
    duration: {requested_seconds: 5, actual_seconds: 5},
    arithmetic: {same_scope: true, ipc_tolerance: 0.005, evidence: 'synthetic-model:1'},
    partition: {confirmed: true, tolerance_pp: 1, evidence: 'synthetic-model:2'},
    pmu: {time_enabled: 5, time_running: 5, minimum_ratio: 0.9, evidence: 'synthetic-pmu:1'},
    work: {same_window: true, units: 100, unit: 'frames', evidence: 'synthetic-progress:1'},
  };
}

const run = (m = manifest(), text = raw, select = {}) => validate(m, inspect(text), select);
const state = (result, id) => result.checks.find(check => check.id === id)?.status;

test('valid arithmetic is a comparison candidate, never a complete A/B verdict', () => {
  const result = run();
  assert.equal(result.quality_status, 'pass');
  assert.equal(result.cycles_per_unit, 10);
  assert.equal(result.comparison_candidate, true);
  assert.equal(result.comparison_eligible, false);
});

test('signed and unavailable values retain their meaning', () => {
  assert.equal(parseValue('-2.5%').value, -2.5);
  assert.equal(parseValue('+1.2e3').value, 1200);
  assert.equal(parseValue('1,234').value, 1234);
  assert.equal(parseValue('1,23').status, 'unrecognized');
  for (const value of ['NaN', '-Inf', 'Infinity', '1e999']) {
    assert.equal(parseValue(value).status, 'nonfinite');
  }
  for (const value of ['N/A', '<not counted>', '<not supported>']) {
    assert.equal(parseValue(value).status, 'unavailable');
  }
});

test('negative result is invalid, never made positive', () => {
  const result = run(manifest(), raw.replace('13.03', '-13.03'));
  assert.equal(result.metrics['Bad Speculation'].value, -13.03);
  assert.equal(result.quality_status, 'invalid');
  assert.equal(result.cycles_per_unit, null);
});

test('large or fractional counts are not silently rounded', () => {
  for (const value of ['9007199254740993', '1000.5']) {
    const result = run(manifest(), raw.replace('Cycles 1,000', `Cycles ${value}`));
    assert.equal(result.metrics.Cycles.status, 'unsafe_count');
    assert.equal(result.metrics.Cycles.value, null);
    assert.equal(result.quality_status, 'invalid');
  }
});

test('selection typos and duplicate row IDs fail explicitly', () => {
  assert.throws(() => run(manifest(), raw, {IPX: 'line-3'}), /Unknown selection/);
  const parsed = inspect(raw);
  parsed.rows[1].id = parsed.rows[0].id;
  assert.throws(() => validate(manifest(), parsed), /duplicate parsed row/);
});

test('framework scope contamination is limited even with valid L1 sum', () => {
  const m = manifest();
  m.scope.value = false;
  assert.equal(run(m).quality_status, 'limited');
});

test('tool success cannot override target crash', () => {
  const m = manifest();
  m.execution.target_exit = -11;
  m.duration.actual_seconds = 0.246;
  const result = run(m);
  assert.equal(result.quality_status, 'invalid');
  assert.equal(state(result, 'duration'), 'limited');
});

test('normally completed short window is not classified as a crash', () => {
  const m = manifest();
  m.duration.actual_seconds = 0.246;
  assert.equal(run(m).quality_status, 'limited');
});

test('missing target exit remains unknown', () => {
  const m = manifest();
  delete m.execution.target_exit;
  assert.equal(run(m).quality_status, 'unknown');
});

test('a healthy persistent service need not exit', () => {
  const m = manifest();
  m.execution.kind = 'service';
  delete m.execution.target_exit;
  m.execution.healthy = {value: true, evidence: 'health:1'};
  assert.equal(run(m).quality_status, 'pass');
  m.execution.healthy.value = false;
  assert.equal(run(m).quality_status, 'invalid');
});

test('whole-task denominator cannot be used for a window', () => {
  const m = manifest();
  m.work.same_window = false;
  assert.equal(run(m).cycles_per_unit, null);
  assert.equal(state(run(m), 'work_denominator'), 'limited');
});

test('unknown PMU scheduling is not invented', () => {
  const m = manifest();
  delete m.pmu;
  assert.equal(state(run(m), 'pmu_scheduling'), 'unknown');
});

test('PMU impossible time relationships and low running ratios', () => {
  const m = manifest();
  m.pmu.time_running = 6;
  assert.equal(state(run(m), 'pmu_scheduling'), 'invalid');
  m.pmu.time_running = 2;
  assert.equal(state(run(m), 'pmu_scheduling'), 'limited');
});

test('unknown model does not receive an automatic L1 partition pass', () => {
  const m = manifest();
  m.partition.confirmed = null;
  assert.equal(state(run(m), 'l1_partition'), 'unknown');
});

test('IPC requires coherent counters and a nonzero denominator', () => {
  assert.equal(state(run(manifest(), raw.replace('IPC 2.00', 'IPC 3.00')), 'ipc_consistency'), 'invalid');
  assert.equal(run(manifest(), raw.replace('Cycles 1,000', 'Cycles 0')).quality_status, 'invalid');
});

test('duplicates require explicit window selection', () => {
  const doubled = raw + '\n' + raw;
  assert.equal(run(manifest(), doubled).quality_status, 'unknown');
  const parsed = inspect(doubled);
  const selected = Object.fromEntries(parsed.rows.slice(7).map(row => [row.name, row.id]));
  assert.equal(validate(manifest(), parsed, selected).quality_status, 'pass');
  assert.throws(() => validate(manifest(), parsed, {IPC: 'line-999'}));
});

test('ignored parameter warning requires review, actual privilege still checked', () => {
  const m = manifest();
  const text = raw + '\nWarning: -r user ignored';
  assert.equal(state(run(m, text), 'warnings_reviewed'), 'unknown');
  m.warnings_reviewed = {value: true, evidence: 'review:1'};
  m.privilege.value = false;
  assert.equal(run(m, text).quality_status, 'limited');
});

test('evidence-free assertions never pass', () => {
  const m = manifest();
  m.identity.evidence = '';
  assert.equal(state(run(m), 'target_identity'), 'unknown');
});

test('empty and unavailable reports do not pass', () => {
  assert.equal(run(manifest(), '').quality_status, 'unknown');
  assert.equal(run(manifest(), raw.replace('2,000', 'N/A')).quality_status, 'invalid');
});

test('report escapes raw HTML and table separators', () => {
  const result = run();
  result.warnings = [{line: 8, text: '<script>x</script>|[click](bad)'}];
  const markdown = report(result);
  assert.ok(!markdown.includes('<script>'));
  assert.ok(markdown.includes('&#124;'));
  assert.ok(markdown.includes('&#91;click'));
});

test('CLI chain writes artifacts, rejects overwrite and malformed input', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'topdown-quality-'));
  const scripts = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const cli = (file, options) => spawnSync(process.execPath, [path.join(scripts, file), ...options], {encoding: 'utf8'});
  try {
    const log = path.join(tmp, 'raw.log');
    const mf = path.join(tmp, 'manifest.json');
    const parsed = path.join(tmp, 'parsed.json');
    const quality = path.join(tmp, 'quality.json');
    const md = path.join(tmp, 'report.md');
    fs.writeFileSync(log, raw);
    fs.writeFileSync(mf, JSON.stringify(manifest()));
    const parseArgs = ['--log', log, '--out', parsed];
    assert.equal(cli('inspect_artifacts.mjs', parseArgs).status, 0);
    assert.equal(cli('inspect_artifacts.mjs', parseArgs).status, 2);
    assert.equal(cli('validate_metrics.mjs', ['--manifest', mf, '--parsed', parsed, '--out', quality]).status, 0);
    assert.equal(cli('build_quality_report.mjs', ['--input', quality, '--out', md]).status, 0);
    assert.ok(fs.readFileSync(md, 'utf8').includes('**pass**'));
    fs.writeFileSync(mf, '{broken');
    assert.equal(cli('validate_metrics.mjs', ['--manifest', mf, '--parsed', parsed, '--out', quality]).status, 2);
    assert.equal(cli('inspect_artifacts.mjs', ['--wrong', log]).status, 2);
  } finally {
    // Remove only the exact private directory created above, never input paths.
    assert.ok(path.basename(tmp).startsWith('topdown-quality-'));
    assert.equal(path.dirname(tmp), path.resolve(os.tmpdir()));
    fs.rmSync(tmp, {recursive: true, force: true});
  }
});
