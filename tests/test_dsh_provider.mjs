import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { apply, inject } from '../adapters/dsh/index.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));

function mount() {
  let provider;
  let mounted = false;
  const effects = [];
  const ctx = {
    skills: {
      registerProvider(create) {
        provider = create({ signal: new AbortController().signal, invalidate() {} });
        mounted = true;
        return () => { mounted = false; };
      },
    },
    effect(effect) { effects.push(effect()); },
  };
  apply(ctx);
  return { provider, mounted: () => mounted, dispose: () => effects.reverse().forEach(effect => effect()) };
}

test('native provider mounts and disposes using the inspected skills service contract', () => {
  assert.deepEqual(inject, ['skills']);
  const instance = mount();
  assert.equal(instance.mounted(), true);
  assert.equal(instance.provider.name, 'kpbot-dsh');
  instance.dispose();
  assert.equal(instance.mounted(), false);
});

test('native provider lists the real package and loads DSH instructions with canonical resources', async () => {
  const { provider, dispose } = mount();
  try {
    const entries = await provider.list();
    const entry = entries.find(entry => entry.name === 'kpbot-code-optimizer');
    assert.ok(entry);
    assert.match(entry.path, /[/\\]dsh[/\\]kpbot-code-optimizer[/\\]SKILL\.md$/);
    assert.equal(entry.resourceBase.kind, 'directory');
    assert.notEqual(path.dirname(entry.path), entry.resourceBase.path);
    assert.ok((await fs.stat(path.join(entry.resourceBase.path, 'scripts', 'experiment_record.py'))).isFile());
    const skill = await provider.get(entry);
    assert.equal(skill.name, entry.name);
    assert.deepEqual(skill.resourceBase, entry.resourceBase);
    assert.match(skill.content, /KPBot 的 DSH 运行协议/);
    assert.match(skill.content, /鲲鹏代码性能优化（DSH 首版）/);
    assert.equal(entries.some(entry => entry.name === 'kpbot-app-tuner'), false);
    for (const excluded of ['drive-claude-optimize-pipeline', 'batch-drive-optimize-pipeline', 'kpbot-code-optimizer-workflow',
      'multimedia-code-optimizer', 'tf-inference-optimizer', 'tf-env-verify', 'tf-profile-collector', 'tf-graph-optimize',
      'tf-kernel-optimize', 'tf-result-verify', 'tf-benchmark']) {
      assert.equal(entries.some(entry => entry.name === excluded), false, excluded);
    }
    assert.equal(new Set(entries.map(entry => entry.name)).size, entries.length);
    const grouped = entries.find(entry => entry.name === 'cubesandbox-create-optimize');
    assert.ok(grouped);
    assert.equal((await provider.get(grouped)).name, grouped.name);
    assert.ok((await fs.stat(path.join(grouped.resourceBase.path, 'references/workflow.md'))).isFile());
  } finally { dispose(); }
});

test('native lookup rejects aborted requests and stale name selections', async () => {
  const { provider, dispose } = mount();
  try {
    const signal = AbortSignal.abort();
    await assert.rejects(provider.list({ signal }), { name: 'AbortError' });
    const entry = (await provider.list()).find(entry => entry.name === 'kpbot-code-optimizer');
    await assert.rejects(provider.get(entry, { signal }), { name: 'AbortError' });
    assert.equal(await provider.get({ ...entry, name: 'renamed-selection' }), undefined);
  } finally { dispose(); }
});

test('DSH entry retains shared experiment gates and uses native async delegation', async () => {
  const entry = await fs.readFile(path.join(root, 'Plugins/code-optimizer/dsh/kpbot-code-optimizer/SKILL.md'), 'utf8');
  for (const required of ['resourceBase', 'subagent({', 'send_message', 'ask_user_question', 'todo_write',
    'experiment_record.py', 'experiment_feedback.py', 'launch_claimed', 'current_context.json',
    'correctness_verdict', 'local_performance_verdict', 'system_acceptance', 'candidate_path',
    'parent_state', 'stop_budget', 'batch_result.json']) assert.ok(entry.includes(required), required);
  assert.doesNotMatch(entry, /(?:Agent|Task|Workflow|task)\s*\(\s*\{/);
  assert.doesNotMatch(entry, /subagent_type\s*:/);
  assert.doesNotMatch(entry, /(?:\.claude|\.opencode|\.dsh)\/skills|Plugins\/code-optimizer/);
  const base = path.join(root, 'Plugins/code-optimizer/skills/kpbot-code-optimizer');
  for (const stage of ['gather-context', 'parse-intent', 'prepare-project', 'decompose-tasks', 'analyze-testcase',
    'analyze-hotspot', 'analyze-caller-context', 'design-experiment', 'validate-experiment',
    'decide-optimization', 'adversarial-review', 'apply-optimization', 'verify-optimization', 'fix-code']) {
    assert.ok((await fs.stat(path.join(base, 'prompts', `stage.${stage}.md`))).isFile(), stage);
  }
});

test('acceptance fixture compiles, passes its full gate and rejects a wrong kernel', async t => {
  const compiler = spawnSync('gcc', ['--version'], { encoding: 'utf8' });
  if (compiler.error?.code === 'ENOENT') return t.skip('gcc is not installed');
  assert.equal(compiler.status, 0, compiler.stderr);
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'kpbot-dsh-fixture-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const fixture = path.join(root, 'tests/fixtures/dsh-optimizer');
  const build = source => {
    const binary = path.join(directory, 'bench');
    const result = spawnSync('gcc', ['-O2', '-std=c11', '-Wall', '-Wextra', '-Werror', source,
      path.join(fixture, 'bench.c'), '-o', binary], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return spawnSync(binary, ['--test'], { encoding: 'utf8' });
  };
  const correct = build(path.join(fixture, 'kernel.c'));
  assert.equal(correct.status, 0, correct.stderr);
  assert.deepEqual(JSON.parse(correct.stdout), { correctness: 'passed', cases: 11 });
  const wrong = path.join(directory, 'wrong.c');
  await fs.writeFile(wrong, '#include <stddef.h>\n#include <stdint.h>\nuint64_t transform_sum(const uint32_t *a, uint32_t *b, size_t n) { (void)a; (void)b; (void)n; return 0; }\n');
  const rejected = build(wrong);
  assert.equal(rejected.status, 1);
  assert.match(rejected.stderr, /correctness failed/);
  const compared = path.join(directory, 'comparison');
  const paired = spawnSync(process.execPath, [path.join(fixture, 'compare.mjs'),
    path.join(fixture, 'kernel.c'), path.join(fixture, 'kernel.c'), compared], { encoding: 'utf8', timeout: 30000 });
  assert.equal(paired.status, 0, paired.stderr);
  const measurements = JSON.parse(paired.stdout);
  assert.equal(measurements.correctness, 'passed');
  assert.equal(measurements.samples.baseline.length, 4);
  assert.equal(measurements.samples.candidate.length, 4);
  assert.ok(measurements.median_seconds.baseline > 0 && measurements.median_seconds.candidate > 0);
  const badOutput = path.join(directory, 'bad-comparison');
  const badPair = spawnSync(process.execPath, [path.join(fixture, 'compare.mjs'),
    path.join(fixture, 'kernel.c'), wrong, badOutput], { encoding: 'utf8', timeout: 30000 });
  assert.notEqual(badPair.status, 0);
  await assert.rejects(fs.stat(path.join(badOutput, 'measurements.json')), { code: 'ENOENT' });
  assert.ok((await fs.stat(path.join(badOutput, 'commands.json'))).isFile());
});

test('bundle manifest and metadata expose installed resources', async () => {
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8'));
  assert.equal(manifest.dsh.bundle.patch, './adapters/dsh/cordis.patch.yml');
  assert.equal(manifest.exports['./dsh'], './adapters/dsh/index.mjs');
  assert.equal(manifest.type, undefined, 'do not change CommonJS semantics of existing business scripts');
  for (const language of ['en', 'zh']) {
    const locale = JSON.parse(await fs.readFile(path.join(root, 'adapters/dsh/locale', `${language}.json`), 'utf8'));
    assert.ok(locale.meta.title && locale.meta.description);
  }
});
