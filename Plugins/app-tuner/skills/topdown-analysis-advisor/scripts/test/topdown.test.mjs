import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import {
  parseText, numeric, collectionArgs, parseOptions,
  capture, analyze, digest, writeNewJson, readLimited,
} from '../topdown.mjs';

const sample = `Top-down metrics of the system:
Cycles              129,522
Instructions        178,765
IPC                 1.38
  Top-down Metrics                        Bound(%)
  Bad Speculation                             3.10
  ├── Branch Mispredicts                      1.89
  Frontend Bound                             10.81
  Retiring                                   17.25
  Backend Bound                              68.84
  ├── Core Bound                             33.76
  └── Memory Bound                           35.08
      ├── L1 Bound                           21.30
      └── Mem Bound                           1.10
  PMU Event                                  Count
  r0008                            178,765
  r0011                            129,522
Note: To view the hotspot data.
`;

function temporary(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'topdown-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test('complete table preserves evidence and does not infer denominator', () => {
  const result = parseText(sample);
  assert.equal(result.status, 'ready_for_review');
  assert.equal(result.blocks.length, 1);
  assert.equal(result.blocks[0].level1_sum, 100);
  const memory = result.blocks[0].metrics.find(row => row.name === 'Memory Bound');
  assert.equal(memory.value, 35.08);
  assert.equal(memory.line, 12);
  assert.equal(memory.denominator, 'tool-defined/unverified');
  assert.ok(memory.tree_prefix.includes('└'));
  assert.equal(result.blocks[0].pmu_events[0].count, '178765');
});

test('CRLF and ANSI color retain values and line numbers', () => {
  const result = parseText(sample.replace('Retiring', '\x1b[32mRetiring\x1b[0m').replaceAll('\n', '\r\n'));
  assert.equal(result.status, 'ready_for_review');
  assert.equal(result.blocks[0].metrics.find(row => row.name === 'Retiring').line, 9);
});

test('multiple windows are never silently aggregated', () => {
  const result = parseText(sample + sample.replace('68.84', '58.84'));
  assert.equal(result.blocks.length, 2);
  assert.equal(result.blocks[0].level1_sum, 100);
  assert.equal(result.blocks[1].level1_sum, 90);
  assert.equal(result.status, 'needs_review');
});

test('missing values stay null', () => {
  const result = parseText(sample.replace('68.84', 'N/A'));
  assert.equal(result.status, 'needs_review');
  assert.equal(result.blocks[0].metrics.find(row => row.name === 'Backend Bound').value, null);
});

test('all missing metrics are unusable without losing original rows', () => {
  const text = `Top-down metrics of the system:
  Top-down Metrics                        Bound(%)
  Bad Speculation                         N/A
  Frontend Bound                          N/A
  Retiring                                N/A
  Backend Bound                           N/A`;
  const result = parseText(text);
  assert.equal(result.status, 'unusable');
  assert.equal(result.blocks[0].metrics.length, 4);
  assert.ok(result.blocks[0].metrics.every(row => row.value === null && row.raw));
  assert.ok(result.blocks[0].warnings.length);
  assert.equal(parseText(text.replace('Retiring                                N/A',
    'Retiring                                0')).status, 'needs_review');
});

test('explicit empty targets are rejected even when the other target is valid', () => {
  for (const target of [
    { pid: '', cpus: '0-3' }, { cpus: '', pid: '123' },
    { pid: ' ' }, { cpus: '' },
  ]) assert.throws(() => collectionArgs({ duration: '1', ...target }), /must not be empty/);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  test(`interruption ${signal} waits for exit, records state and removes handlers`, async t => {
    const directory = temporary(t);
    const before = process.listenerCount(signal);
    const pending = capture(process.execPath, ['-e', 'setInterval(()=>{}, 1000)'], directory, 5000);
    process.emit(signal);
    const result = await pending;
    assert.equal(result.status, 'interrupted');
    assert.equal(result.interrupted_by, signal);
    assert.equal(result.timed_out, false);
    assert.equal(process.listenerCount(signal), before);
    writeNewJson(path.join(directory, 'manifest.json'), result);
    assert.equal(JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'))).status, 'interrupted');
  });
}

test('evaluation queries use the repository object convention', () => {
  const data = JSON.parse(fs.readFileSync(new URL('../../evals/eval_queries.json', import.meta.url)));
  assert.equal(data.skill_name, 'topdown-analysis-advisor');
  assert.equal(data.queries.length, 8);
  assert.ok(data.queries.every(row => row.id && row.query && row.expected));
});

test('unknown format does not produce plausible percentages', () => {
  assert.equal(parseText('frontend_bound 0.3\nbackend_bound 0.7').status, 'unusable');
  assert.equal(parseText('').blocks.length, 0);
});

test('partial profile is retained with completeness warning', () => {
  const result = parseText(sample.replace(/  Retiring[^\n]+\n/, ''));
  assert.equal(result.status, 'needs_review');
  assert.ok(result.blocks[0].warnings.some(row => row.code === 'root_count'));
});

test('duplicates, invalid ranges and zero counters are flagged', () => {
  for (const changed of [
    sample.replace('17.25', '117.25'),
    sample.replace('17.25', '-1.00'),
    sample.replace('129,522', '0'),
    sample.replace('  Retiring', '  Retiring           17.25\n  Retiring'),
  ]) assert.equal(parseText(changed).status, 'needs_review');
});

test('unrecognized metric tokens and tool errors are not discarded silently', () => {
  const result = parseText(sample.replace('35.08', '??') + 'error: PMU failed');
  assert.equal(result.status, 'needs_review');
  assert.ok(result.warnings.some(row => row.code === 'tool_diagnostic'));
  assert.ok(result.blocks[0].warnings.some(row => row.code === 'unparsed_metric_line'));
});

test('PMU counts preserve integer precision', () => {
  const result = parseText(sample.replace('r0008                            178,765',
    'r0008                            90,071,992,547,409,931'));
  assert.equal(result.blocks[0].pmu_events[0].count, '90071992547409931');
});

test('deep tree prefixes are not mistaken for column delimiters', () => {
  const result = parseText(sample.replace('      ├── L1 Bound', '  │   │   ├── L1 Bound'));
  const row = result.blocks[0].metrics.find(item => item.name === 'L1 Bound');
  assert.equal(row.value, 21.30);
  assert.equal(row.tree_prefix, '  │   │   ├── ');
  assert.equal(result.status, 'ready_for_review');
});

test('numeric tokens require a complete valid value', () => {
  assert.equal(numeric('1,234.50'), 1234.5);
  assert.equal(numeric('N/A'), null);
  assert.equal(numeric('12abc'), undefined);
  assert.equal(numeric('1,23'), undefined);
  assert.equal(numeric('Infinity'), undefined);
});

test('collection argv uses explicit scope and one target', () => {
  const args = collectionArgs({ duration: '30', pid: '123,456' });
  assert.deepEqual(args.slice(-2), ['-p', '123,456']);
  assert.ok(args.includes('30'));
  assert.deepEqual(collectionArgs({ duration: '1', cpus: '0-3,8' }).slice(-2), ['-c', '0-3,8']);
});

test('invalid collection parameters fail before any command', () => {
  for (const options of [
    { duration: '0', pid: '1' },
    { duration: '3601', pid: '1' },
    { duration: '1', interval: '2', pid: '1' },
    { duration: '1', pid: '1; echo x' },
    { duration: '1', pid: 'ALL' },
    { duration: '1', cpus: '3-0' },
    { duration: '1', pid: '1', cpus: '0' },
    { duration: '1', level: '7', pid: '1' },
    { duration: '1', scope: 'other', pid: '1' },
    { duration: '1' },
  ]) assert.throws(() => collectionArgs(options));
});

test('CLI rejects duplicates, unknown and incomplete arguments', () => {
  for (const args of [['--x'], ['--y', '1'], ['--x', '1', '--x', '2'], ['x', '1']]) {
    assert.throws(() => parseOptions(args, ['x']));
  }
});

test('JSON writes refuse overwrite', t => {
  const file = path.join(temporary(t), 'result.json');
  writeNewJson(file, { first: true });
  assert.throws(() => writeNewJson(file, { first: false }));
  assert.equal(JSON.parse(fs.readFileSync(file)).first, true);
});

test('analyze validates manifest identity and failed collection', t => {
  const directory = temporary(t);
  const input = path.join(directory, 'stdout.txt');
  const manifest = path.join(directory, 'manifest.json');
  fs.writeFileSync(input, sample);
  writeNewJson(manifest, { status: 'failed', exit_code: 1,
    files: [{ file: 'stdout.txt', sha256: 'wrong' }] });
  const result = analyze({ input, manifest, output: path.join(directory, 'result.json') });
  assert.equal(result.status, 'needs_review');
  assert.equal(result.source.sha256, digest(Buffer.from(sample)));
  assert.deepEqual(result.warnings.map(row => row.code), ['manifest_mismatch', 'collection_failed']);
});

test('capture saves stderr, exit status and literal arguments', async t => {
  const directory = temporary(t);
  const literal = 'a; echo should-not-run';
  const result = await capture(process.execPath,
    ['-e', 'console.log(process.argv[1]); console.error("warning"); process.exitCode=3;', literal],
    directory, 5000);
  assert.equal(result.status, 'failed');
  assert.equal(result.exit_code, 3);
  assert.equal(fs.readFileSync(path.join(directory, 'stdout.txt'), 'utf8').trim(), literal);
  assert.match(fs.readFileSync(path.join(directory, 'stderr.txt'), 'utf8'), /warning/);
});

test('capture bounds a hung process', async t => {
  const result = await capture(process.execPath, ['-e', 'setInterval(()=>{}, 1000)'], temporary(t), 150);
  assert.equal(result.status, 'timeout');
  assert.equal(result.timed_out, true);
});

test('missing executable is recorded as failure', async t => {
  const result = await capture(path.join(temporary(t), 'missing-executable'), [], temporary(t), 1000);
  assert.equal(result.status, 'failed');
  assert.ok(result.error);
});

test('input size limit is enforced', t => {
  const file = path.join(temporary(t), 'large.txt');
  fs.writeFileSync(file, '');
  fs.truncateSync(file, 33 * 1024 * 1024);
  assert.throws(() => readLimited(file), /32 MiB/);
});

test('real CLI writes JSON and returns review status', t => {
  const directory = temporary(t);
  const input = path.join(directory, 'input.txt');
  const output = path.join(directory, 'output.json');
  fs.writeFileSync(input, sample.replace('68.84', 'N/A'));
  const script = fileURLToPath(new URL('../topdown.mjs', import.meta.url));
  const result = spawnSync(process.execPath,
    [script, 'analyze', '--input', input, '--output', output], { encoding: 'utf8' });
  assert.equal(result.status, 2);
  assert.equal(JSON.parse(fs.readFileSync(output)).status, 'needs_review');
});
