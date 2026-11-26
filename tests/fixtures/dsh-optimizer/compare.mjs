// Fixed acceptance command: compile both variants, gate correctness, then ABBA.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const [baseline, candidate, output] = process.argv.slice(2);
assert.ok(baseline && candidate && output, 'usage: node compare.mjs BASE_KERNEL CANDIDATE_KERNEL NEW_OUTPUT_DIR');
const directory = path.resolve(output);
await fs.mkdir(directory); // refuse to overwrite previous experiment evidence
const harness = fileURLToPath(new URL('bench.c', import.meta.url));
const commands = [];
function execute(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 30000 });
  commands.push({ command, args, exit_code: result.status, signal: result.signal,
    stdout: result.stdout ?? '', stderr: result.stderr ?? '' });
  assert.equal(result.status, 0, `${command} failed: ${result.stderr ?? result.error?.message}`);
  return result.stdout;
}
const median = values => {
  const sorted = [...values].sort((a, b) => a - b);
  return (sorted[(sorted.length - 1) >> 1] + sorted[sorted.length >> 1]) / 2;
};
try {
  for (const [name, source] of [['baseline', baseline], ['candidate', candidate]]) {
    const binary = path.join(directory, name);
    execute('gcc', ['-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', path.resolve(source), harness, '-o', binary]);
    assert.deepEqual(JSON.parse(execute(binary, ['--test'])), { correctness: 'passed', cases: 11 });
  }
  const order = ['baseline', 'candidate', 'candidate', 'baseline', 'baseline', 'candidate', 'candidate', 'baseline'];
  const samples = { baseline: [], candidate: [] };
  for (const side of order) {
    const measured = JSON.parse(execute(path.join(directory, side), ['--bench']));
    assert.ok(Number.isFinite(measured.seconds) && measured.seconds > 0, 'positive timing required');
    assert.equal(measured.elements, 1048576);
    assert.equal(measured.repeats, 128);
    samples[side].push(measured.seconds);
  }
  const result = { correctness: 'passed', cases: 11, order, samples,
    median_seconds: { baseline: median(samples.baseline), candidate: median(samples.candidate) } };
  result.improvement_fraction = 1 - result.median_seconds.candidate / result.median_seconds.baseline;
  await fs.writeFile(path.join(directory, 'measurements.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result));
} finally {
  await fs.writeFile(path.join(directory, 'commands.json'), JSON.stringify(commands, null, 2) + '\n');
}
