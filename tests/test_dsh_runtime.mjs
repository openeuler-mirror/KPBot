// Opt-in installed-DSH integration in a fresh in-process Cordis host.
// No profile, session, credential store, model or network is loaded.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import * as kpbot from '../adapters/dsh/index.mjs';
import { installSkills, uninstallSkills } from '../adapters/dsh/install.mjs';

const repository = fileURLToPath(new URL('../', import.meta.url));
const installation = process.env.DSH_INSTALL_DIR;
let runtime;

async function installedRuntime() {
  if (runtime) return runtime;
  const cliManifest = JSON.parse(await fs.readFile(path.join(installation, 'package.json'), 'utf8'));
  assert.equal(cliManifest.name, '@deepseek-ai/dsh');
  const require = createRequire(path.join(installation, 'package.json'));
  const load = name => import(pathToFileURL(require.resolve(name)).href);
  const [cordis, registry, filesystem] = await Promise.all([
    load('@deepseek-ai/cordis'), load('@deepseek-ai/dsh-skill'), load('@deepseek-ai/dsh-skill-filesystem'),
  ]);
  runtime = { Context: cordis.Context, Registry: registry.default, filesystem, version: cliManifest.version };
  return runtime;
}

test('isolated real DSH registry accepts and unloads the native KPBot provider', { skip: !installation }, async () => {
  const { Context, Registry } = await installedRuntime();
  const host = new Context();
  try {
    await host.plugin(Registry);
    const fiber = await host.plugin(kpbot);
    const snapshot = await host.skills.snapshot();
    assert.equal(snapshot.complete, true);
    assert.ok(snapshot.skills.some(skill => skill.name === 'kpbot-code-optimizer'));
    const entry = await host.skills.get('kpbot-code-optimizer');
    assert.match(entry.content, /KPBot 的 DSH 运行协议/);
    assert.match(entry.content, /subagent\(\{/);
    assert.ok((await fs.stat(path.join(entry.resourceBase.path, 'scripts/experiment_record.py'))).isFile());
    await fiber.dispose();
    assert.deepEqual(await host.skills.list(), []);
  } finally { await host.fiber.dispose(); }
});

test('isolated real DSH filesystem loads installed skills without scanning the current session roots', { skip: !installation }, async () => {
  const { Context, Registry, filesystem } = await installedRuntime();
  const parent = path.join(repository, '.runtime');
  await fs.mkdir(parent, { recursive: true });
  const directory = await fs.mkdtemp(path.join(parent, 'dsh-runtime-'));
  const host = new Context();
  const options = { repositoryRoot: repository, level: 'project', installPath: path.join(directory, 'workspace') };
  try {
    const installed = await installSkills(options);
    await host.plugin(Registry);
    await host.plugin(filesystem, {
      providerName: 'kpbot-isolated-acceptance', includeDefaultRoots: false,
      customSkillDirs: [path.join(installed.root, 'skills')], watch: false,
    });
    const catalog = await host.skills.list({ cwd: path.join(directory, 'workspace') });
    assert.equal(catalog.length, installed.installed.length);
    const entry = await host.skills.get('kpbot-code-optimizer');
    assert.match(entry.content, /鲲鹏代码性能优化（DSH 首版）/);
    assert.match(entry.content, /向量检索.*vector-search-optimize/);
    assert.match(entry.content, /OpenZL.*openzl-compression-tuning/);
    assert.doesNotMatch(entry.content, /BASE\/SKILL\.md/, 'copied entry must not resolve the canonical routing table through itself');
    assert.ok(entry.resourceBase.path.startsWith(directory + path.sep));
    assert.ok((await fs.stat(path.join(entry.resourceBase.path, 'scripts/experiment_feedback.py'))).isFile());
    await uninstallSkills(options);
  } finally {
    await host.fiber.dispose();
    // directory is a fresh mkdtemp result under the test-owned runtime parent.
    await fs.rm(directory, { recursive: true, force: true });
  }
});
