import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { planOutputs, publishOutputs } from '../scripts/output.mjs';

const cli = fileURLToPath(new URL('../scripts/diagram.mjs', import.meta.url));
const extensions = ['.excalidraw', '.svg', '.png', '.graph.json', '.qa.json'];
const content = version => extensions.map(ext => version + ext);

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'diagram-outputs-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const files = extensions.map(ext => path.join(directory, 'drawing' + ext));
  const input = path.join(directory, 'source.json');
  await fs.writeFile(input, JSON.stringify({ nodes: [{ id: 'a', label: 'A' }], edges: [] }));
  return { directory, input, files };
}

async function expectGroup(files, expected) {
  for (const [i, file] of files.entries()) {
    if (expected[i] === null) await assert.rejects(fs.lstat(file), { code: 'ENOENT' });
    else assert.equal(await fs.readFile(file, 'utf8'), expected[i], file);
  }
}

test('force rejects hard links to the input and between outputs', async t => {
  const { input, files } = await fixture(t);
  const original = await fs.readFile(input, 'utf8');
  await fs.link(input, files[3]);
  const run = spawnSync(process.execPath, [cli, '--input', input, '--output', files[0], '--force'], { encoding: 'utf8' });
  assert.equal(run.status, 1);
  assert.match(run.stderr, /aliases/);
  await expectGroup(files, [null, null, null, original, null]);
  await fs.unlink(files[3]);
  await fs.writeFile(files[0], 'previous');
  await fs.link(files[0], files[4]);
  await assert.rejects(planOutputs(input, files, true), /aliases/);
  await expectGroup(files, ['previous', null, null, null, 'previous']);
  assert.equal(await fs.readFile(input, 'utf8'), original);
});

test('force rejects symbolic links, dangling outputs and non-file outputs', async t => {
  const { directory, input, files } = await fixture(t);
  await fs.mkdir(files[4]);
  await assert.rejects(planOutputs(input, files, true), /regular file/);
  await fs.rmdir(files[4]);
  try { await fs.symlink(input, files[4]); }
  catch (error) { if (error.code === 'EPERM') return t.skip('symlinks unavailable'); throw error; }
  await assert.rejects(planOutputs(input, files, true), /regular file/);
  await fs.unlink(files[4]);
  await fs.symlink(path.join(directory, 'missing'), files[4]);
  await assert.rejects(planOutputs(input, files, true), /regular file/);
});

test('PNG encoding failure preserves every previous artifact', async t => {
  const { directory, input, files } = await fixture(t);
  const old = content('old');
  await Promise.all(files.map((file, i) => fs.writeFile(file, old[i])));
  const preload = path.join(directory, 'encoder-failure.cjs');
  const canvas = createRequire(import.meta.url).resolve('@napi-rs/canvas');
  await fs.writeFile(preload, `require(${JSON.stringify(canvas)}).CanvasElement.prototype.encode = () => { throw new Error('fixture PNG failure'); };\n`);
  const run = spawnSync(process.execPath, ['--require', preload, cli, '--input', input, '--output', files[0], '--force'], { encoding: 'utf8' });
  assert.equal(run.status, 1);
  assert.match(run.stderr, /fixture PNG failure/);
  await expectGroup(files, old);
});

test('staging failure leaves the previous set intact', async t => {
  const { input, files } = await fixture(t);
  const old = content('old');
  await Promise.all(files.map((file, i) => fs.writeFile(file, old[i])));
  const planned = await planOutputs(input, files, true);
  const data = content('new');
  data[2] = null;
  await assert.rejects(publishOutputs(planned, data), { code: 'ERR_INVALID_ARG_TYPE' });
  await expectGroup(files, old);
});

for (const code of ['EPERM', 'ENOTSUP']) test(`filesystems reporting ${code} for hard links can export, update and roll back`, async t => {
  const { directory, input, files } = await fixture(t);
  t.mock.method(fs, 'link', async () => { throw Object.assign(new Error('hard links unavailable'), { code }); });
  await publishOutputs(await planOutputs(input, files, false), content('first'));
  await expectGroup(files, content('first'));
  await publishOutputs(await planOutputs(input, files, true), content('second'));
  await expectGroup(files, content('second'));

  const rename = fs.rename;
  const failure = t.mock.method(fs, 'rename', async (from, to) => {
    if (path.basename(from) === 'new-2' && to === files[2]) {
      failure.mock.restore();
      throw new Error('fixture replacement failure');
    }
    return rename(from, to);
  });
  await assert.rejects(publishOutputs(await planOutputs(input, files, true), content('third')), /fixture replacement failure/);
  await expectGroup(files, content('second'));
  assert.deepEqual((await fs.readdir(directory)).sort(), [input, ...files].map(file => path.basename(file)).sort());
});

test('a late file wins exclusive creation on a filesystem without hard links', async t => {
  const { directory, input, files } = await fixture(t);
  const planned = await planOutputs(input, files, true);
  t.mock.method(fs, 'link', async (_from, to) => {
    if (to === files[3]) await fs.writeFile(to, 'late file', { flag: 'wx' });
    throw Object.assign(new Error('hard links unavailable'), { code: 'ENOTSUP' });
  });
  await assert.rejects(publishOutputs(planned, content('new')), { code: 'EEXIST' });
  await expectGroup(files, [null, null, null, 'late file', null]);
  assert.deepEqual((await fs.readdir(directory)).sort(), [path.basename(input), path.basename(files[3])].sort());
});

test('late publication failure restores old files, removes fresh ones and permits retry', async t => {
  const { input, files } = await fixture(t);
  const old = ['old scene', null, 'old PNG', 'old graph', 'old QA'];
  for (const [i, value] of old.entries()) if (value !== null) await fs.writeFile(files[i], value);
  const planned = await planOutputs(input, files, true);
  const link = fs.link;
  const failOnce = t.mock.method(fs, 'link', async (from, to) => {
    if (to === files[3]) {
      failOnce.mock.restore();
      throw new Error('fixture publish failure');
    }
    return link(from, to);
  });
  await assert.rejects(publishOutputs(planned, content('new')), /fixture publish failure/);
  await expectGroup(files, old);
  await publishOutputs(await planOutputs(input, files, true), content('retry'));
  await expectGroup(files, content('retry'));
});

test('concurrent exporters publish one complete set and preserve unrelated late files', async t => {
  const { input, files } = await fixture(t);
  const planned = await planOutputs(input, files, true);
  const outcomes = await Promise.allSettled(['first', 'second'].map(v => publishOutputs(planned, content(v))));
  assert.equal(outcomes.filter(result => result.status === 'fulfilled').length, 1);
  const winner = outcomes[0].status === 'fulfilled' ? 'first' : 'second';
  await expectGroup(files, content(winner));
  const update = await planOutputs(input, files, true);
  await fs.writeFile(files[3], 'external edit');
  await assert.rejects(publishOutputs(update, content('update')), /changed during export/);
  const expected = content(winner);
  expected[3] = 'external edit';
  await expectGroup(files, expected);
});
