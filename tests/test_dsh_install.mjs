import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { discoverSkills, readSkill } from '../adapters/dsh/catalog.mjs';
import { installSkills, uninstallSkills, configRoot, main } from '../adapters/dsh/install.mjs';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const gitBash = path.join(process.env.ProgramFiles || 'C:/Program Files', 'Git/bin/bash.exe');
const bash = process.platform === 'win32' && existsSync(gitBash) ? gitBash : 'bash';
const shellPath = value => process.platform === 'win32' ? value.replace(/\\/g, '/').replace(/^([A-Za-z]):/, (_, drive) => `/${drive.toLowerCase()}`) : value;
async function linkDirectory(target, link) { await fs.symlink(path.resolve(target), link, process.platform === 'win32' ? 'junction' : 'dir'); }
const markdown = (name = 'example', extra = '', body = 'Skill body.') => `---\nname: ${name}\ndescription: >\n  A folded\n  description.\n${extra}---\n${body}\n`;
async function put(file, text) { await fs.mkdir(path.dirname(file), { recursive: true }); await fs.writeFile(file, text); }
async function json(file, data) { await put(file, JSON.stringify(data)); }
async function exists(file) { try { await fs.lstat(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } }
async function snapshot(directory) {
  const result = {};
  async function visit(current) {
    for (const entry of (await fs.readdir(current, { withFileTypes: true })).sort((first, second) => first.name.localeCompare(second.name))) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) await visit(target);
      else result[path.relative(directory, target)] = (await fs.readFile(target)).toString('base64');
    }
  }
  await visit(directory);
  return result;
}
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'kpbot-dsh-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await put(path.join(root, 'adapters/dsh/runtime.md'), '---\ntitle: Runtime\n---\nDSH runtime instructions.\n');
  const project = path.join(root, 'project with spaces');
  const options = { repositoryRoot: root, installPath: project };
  return { root, project, options, config: path.join(project, '.dsh') };
}
async function shellFixture(t) {
  const result = await fixture(t);
  for (const relative of ['adapters/dsh/catalog.mjs', 'adapters/dsh/install.mjs', 'install.sh', 'bin/kpbot', 'bin/_uninstall.sh']) {
    await fs.mkdir(path.dirname(path.join(result.root, relative)), { recursive: true });
    await fs.copyFile(path.join(repository, relative), path.join(result.root, relative));
  }
  await linkDirectory(path.join(repository, 'node_modules'), path.join(result.root, 'node_modules'));
  await json(path.join(result.root, 'package.json'), { version: 'test' });
  return result;
}
async function plugin(root, relative = 'arbitrary/deep/package', metadata = { platforms: ['dsh'] }) {
  const directory = path.join(root, 'Plugins', relative);
  await json(path.join(directory, '.claude-plugin/plugin.json'), metadata);
  return directory;
}
async function skill(directory, skillDir = 'example', name = 'example', extra = '', body) {
  const target = path.join(directory, 'skills', skillDir);
  await put(path.join(target, 'SKILL.md'), markdown(name, extra, body));
  return target;
}

// --- Decoupling: the reason the adapter exists -------------------------------

test('finds opted-in packages at any depth and survives moves and splits', async t => {
  const { root } = await fixture(t);
  const first = await plugin(root);
  await skill(first, 'kunpeng_microarch', 'kunpeng-microarch');
  await skill(first, 'second', 'second');
  let entries = await discoverSkills(root);
  assert.deepEqual(entries.map(entry => entry.name), ['kunpeng-microarch', 'second']);
  assert.equal(entries[0].resourceDir, path.join(first, 'skills/kunpeng_microarch'));

  // A moved directory and a package split must not need adapter changes.
  const moved = path.join(root, 'Plugins/another/a/b/c/moved');
  await fs.mkdir(path.dirname(moved), { recursive: true });
  await fs.rename(first, moved);
  const split = await plugin(root, 'new/domain/split');
  await fs.mkdir(path.join(split, 'skills'));
  await fs.rename(path.join(moved, 'skills/second'), path.join(split, 'skills/second'));

  entries = await discoverSkills(root);
  assert.deepEqual(new Set(entries.map(entry => entry.name)), new Set(['kunpeng-microarch', 'second']));
  assert.ok(entries.every(entry => entry.pluginRoot === moved || entry.pluginRoot === split));
});

test('discovers grouped skills with canonical resources and instruction overlays', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  const base = await skill(directory, 'infrastructure/cube', 'cube');
  await put(path.join(base, 'references/contract.md'), 'Canonical grouped reference');
  await put(path.join(base, 'scripts/check.py'), 'print("fixture")');
  await put(path.join(directory, 'dsh/cube/SKILL.md'), markdown('cube', '', 'Grouped overlay'));
  const [entry] = await discoverSkills(root);
  assert.equal(entry.resourceDir, base);
  assert.equal(entry.skillDir, 'cube');
  assert.equal(entry.path, path.join(directory, 'dsh/cube/SKILL.md'));
  const native = await readSkill(entry, root);
  assert.match(native.content, /Grouped overlay/);
  const installed = await installSkills(options);
  assert.deepEqual(installed.installed, ['cube']);
  assert.equal(await fs.readFile(path.join(config, 'skills/cube/references/contract.md'), 'utf8'), 'Canonical grouped reference');
  assert.match(await fs.readFile(path.join(config, 'skills/cube/SKILL.md'), 'utf8'), /DSH runtime instructions\.\n\nGrouped overlay/);
  assert.equal(await exists(path.join(config, 'skills/infrastructure')), false);
  await uninstallSkills(options);
  assert.equal(await exists(path.join(config, 'skills/cube')), false);
  assert.equal(await exists(path.join(base, 'scripts/check.py')), true);
});

test('group and nested exclusions apply without treating excluded groups as malformed skills', async context => {
  const { root } = await fixture(context);
  const directory = await plugin(root, 'package', { platforms: ['dsh'], dsh: { exclude: ['blocked-group', 'blocked-child'] } });
  await skill(directory, 'blocked-group/hidden', 'hidden');
  await skill(directory, 'allowed-group/blocked-child', 'blocked-child');
  await skill(directory, 'allowed-group/kept', 'kept');
  await skill(directory, 'only-excluded/blocked-child', 'blocked-child');
  assert.deepEqual((await discoverSkills(root)).map(entry => entry.name), ['kept']);
});

test('a skill entry remains the resource boundary for embedded skills', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  const base = await skill(directory, 'bundle', 'bundle');
  await put(path.join(base, 'skills/embedded/SKILL.md'), markdown('embedded'));
  assert.deepEqual((await discoverSkills(root)).map(entry => entry.name), ['bundle']);
  await installSkills(options);
  assert.equal(await exists(path.join(config, 'skills/bundle/skills/embedded/SKILL.md')), true);
  assert.equal(await exists(path.join(config, 'skills/embedded')), false);
});

test('grouped duplicates and missing entries fail before installation writes', async context => {
  for (const duplicateDirectory of [false, true]) {
    const { root, options, config } = await fixture(context);
    const directory = await plugin(root);
    await skill(directory, 'cube', 'cube');
    await skill(directory, `group/${duplicateDirectory ? 'cube' : 'other'}`, duplicateDirectory ? 'other' : 'cube');
    await assert.rejects(installSkills(options), duplicateDirectory ? /Duplicate skill directory/ : /Duplicate skill name/);
    assert.equal(await exists(config), false);
  }
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  await put(path.join(directory, 'skills/broken/references/guide.md'), 'No entry point');
  await assert.rejects(installSkills(options), /Missing SKILL.md/);
  assert.equal(await exists(config), false);
});

test('documentation-only domains are skipped without hiding incomplete skills', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  await skill(directory, 'example');
  await put(path.join(directory, 'skills/storage/DOMAIN.md'), 'Domain without members');
  await installSkills(options);
  assert.equal(await exists(path.join(config, 'skills/example/SKILL.md')), true);
  assert.equal(await exists(path.join(config, 'skills/storage')), false);
  const before = await snapshot(config);
  await put(path.join(directory, 'skills/storage/references/guide.md'), 'Remaining resources');
  await assert.rejects(installSkills(options), /Missing SKILL.md/);
  assert.deepEqual(await snapshot(config), before);
});

test('grouped directory links cannot redirect discovery outside the package', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  const outside = path.join(root, 'outside');
  await put(path.join(outside, 'SKILL.md'), markdown('outside'));
  await fs.mkdir(path.join(directory, 'skills/group'), { recursive: true });
  await linkDirectory(outside, path.join(directory, 'skills/group/linked'));
  await assert.rejects(installSkills(options), /Symlink not allowed/);
  assert.equal(await exists(config), false);
  assert.equal(await exists(path.join(outside, 'SKILL.md')), true);
});

test('installs only packages that name dsh, and never scans inside a package', async t => {
  const { root } = await fixture(t);
  for (const [index, metadata] of [{}, { platforms: ['unknown'] }, { platforms: ['claude', 'opencode'] }].entries()) {
    await skill(await plugin(root, `package-${index}`, metadata));
  }
  const unsupported = await plugin(root, 'unsupported', { platforms: ['claude'] });
  // Resource trees of unsupported packages may contain links; discovery must not enter them.
  await linkDirectory('/does/not/exist', path.join(unsupported, 'vendored-resource'));
  assert.deepEqual(await discoverSkills(root), []);
});

// --- Install contract --------------------------------------------------------

test('overlay replaces only the instruction file and keeps canonical resources', async t => {
  const { root, options, config } = await fixture(t);
  const directory = await plugin(root, 'domain/package', { platforms: ['dsh'], dsh: { exclude: ['skip'] } });
  const group = path.join(directory, 'skills/group');
  await put(path.join(group, 'DOMAIN.md'), '# Domain');
  const base = path.join(group, 'kunpeng_microarch');
  await put(path.join(base, 'SKILL.md'), markdown('kunpeng-microarch'));
  await skill(directory, 'skip', 'skip');
  await put(path.join(base, 'references/file.txt'), 'base resource');
  await put(path.join(base, 'agents/worker.md'), 'prompt resource');
  await put(path.join(directory, 'dsh/kunpeng_microarch/SKILL.md'),
    markdown('kunpeng-microarch', 'disable-model-invocation: true\nuser-invocable: false\n', 'Overlay body.'));

  const [entry] = await discoverSkills(root);
  assert.equal(entry.name, 'kunpeng-microarch');
  assert.equal(entry.path, path.join(directory, 'dsh/kunpeng_microarch/SKILL.md'));
  assert.equal(entry.resourceDir, base);
  const read = await readSkill(entry, root);
  assert.equal(read.description, 'A folded description.');
  assert.deepEqual(read.invocation, { modelInvocable: false, userInvocable: false });
  assert.equal(read.content, 'DSH runtime instructions.\n\nOverlay body.');

  await installSkills(options);
  const installed = path.join(config, 'skills/kunpeng_microarch');
  assert.equal(await fs.readFile(path.join(installed, 'references/file.txt'), 'utf8'), 'base resource');
  assert.equal(await exists(path.join(config, 'agents')), false);
  assert.match(await fs.readFile(path.join(installed, 'SKILL.md'), 'utf8'), /DSH runtime instructions\.\n\nOverlay body\./);
});

test('resource overlays are rejected so native and copied installs cannot diverge', async t => {
  const { root, options, config } = await fixture(t);
  const directory = await plugin(root);
  await skill(directory);
  await put(path.join(directory, 'dsh/example/SKILL.md'), markdown());
  await put(path.join(directory, 'dsh/example/references/platform.md'), 'Unsupported override.');
  await assert.rejects(discoverSkills(root), /Only SKILL.md overlays are supported/);
  await assert.rejects(installSkills(options), /Only SKILL.md overlays are supported/);
  assert.equal(await exists(config), false);
});

test('reinstall and uninstall never touch user skills', async t => {
  const { root, options, config } = await fixture(t);
  const directory = await plugin(root);
  await skill(directory, 'old', 'old');
  await put(path.join(config, 'skills/user/SKILL.md'), 'User content');
  await installSkills(options);
  await installSkills(options);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(config, 'kpbot-manifest.json'))).installed_skills, ['old']);

  await fs.rm(path.join(directory, 'skills/old'), { recursive: true });
  await skill(directory, 'new', 'new');
  await installSkills(options);
  assert.equal(await exists(path.join(config, 'skills/old')), false);
  assert.equal(await fs.readFile(path.join(config, 'skills/user/SKILL.md'), 'utf8'), 'User content');

  assert.deepEqual((await uninstallSkills(options)).removed, ['new']);
  assert.equal(await exists(path.join(config, 'skills/user/SKILL.md')), true);
  assert.equal(await exists(path.join(config, 'kpbot-manifest.json')), false);
});

for (const phase of ['backup', 'publish', 'manifest']) {
  test(`failed ${phase} publication restores all previous DSH files and retired skills`, async context => {
    const { root, options, config } = await fixture(context);
    const directory = await plugin(root);
    await skill(directory);
    await skill(directory, 'retired', 'retired');
    await installSkills(options);
    await put(path.join(config, 'skills/user/SKILL.md'), 'User content');
    const before = await snapshot(config);
    await fs.rm(path.join(directory, 'skills/retired'), { recursive: true });
    await skill(directory, 'another', 'another');
    await skill(directory, 'example', 'example', '', 'Updated instructions');
    const rename = fs.rename;
    let failed = false;
    const injected = context.mock.method(fs, 'rename', async (source, destination) => {
      if (!failed && ((phase === 'backup' && path.basename(path.dirname(destination)) === 'old' && path.basename(source) === 'retired') ||
          (phase === 'publish' && destination === path.join(config, 'skills/example')) ||
          (phase === 'manifest' && destination === path.join(config, 'kpbot-manifest.json')))) {
        failed = true;
        throw new Error(`Fixture ${phase} failure`);
      }
      return rename(source, destination);
    });
    try { await assert.rejects(installSkills(options), new RegExp(`Fixture ${phase} failure`)); }
    finally { injected.mock.restore(); }
    assert.deepEqual(await snapshot(config), before);
    assert.equal((await fs.readdir(config)).some(name => name.startsWith('.kpbot-dsh-stage-')), false);
  });
}

test('failed staged copies preserve previous DSH files and clean partial resources', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  await skill(directory);
  await installSkills(options);
  const before = await snapshot(config);
  const copy = fs.cp;
  const injected = context.mock.method(fs, 'cp', async (...argumentsList) => {
    await copy(...argumentsList);
    throw new Error('Fixture partial copy failure');
  });
  try { await assert.rejects(installSkills(options), /Fixture partial copy failure/); }
  finally { injected.mock.restore(); }
  assert.deepEqual(await snapshot(config), before);
});

test('failed initial DSH manifest publication leaves user skills intact and can be retried', async context => {
  const { root, options, config } = await fixture(context);
  await skill(await plugin(root));
  await put(path.join(config, 'skills/user/SKILL.md'), 'User content');
  const before = await snapshot(config);
  const rename = fs.rename;
  const injected = context.mock.method(fs, 'rename', async (source, destination) => {
    if (destination === path.join(config, 'kpbot-manifest.json')) throw new Error('Fixture first publication failure');
    return rename(source, destination);
  });
  try { await assert.rejects(installSkills(options), /Fixture first publication failure/); }
  finally { injected.mock.restore(); }
  assert.deepEqual(await snapshot(config), before);
  await installSkills(options);
  assert.equal(await exists(path.join(config, 'skills/example/SKILL.md')), true);
});

test('failed DSH rollback retains recovery files and restores every other skill', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  await skill(directory);
  await skill(directory, 'retired', 'retired');
  await installSkills(options);
  const original = await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8');
  const manifest = await fs.readFile(path.join(config, 'kpbot-manifest.json'), 'utf8');
  await skill(directory, 'example', 'example', '', 'Updated instructions');
  const rename = fs.rename;
  const injected = context.mock.method(fs, 'rename', async (source, destination) => {
    if (destination === path.join(config, 'kpbot-manifest.json') ||
        (path.basename(path.dirname(source)) === 'old' && path.basename(source) === 'retired')) {
      throw new Error('Fixture recovery failure');
    }
    return rename(source, destination);
  });
  try { await assert.rejects(installSkills(options), /recovery files preserved in/); }
  finally { injected.mock.restore(); }
  assert.equal(await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8'), original);
  assert.equal(await fs.readFile(path.join(config, 'kpbot-manifest.json'), 'utf8'), manifest);
  const stages = (await fs.readdir(config)).filter(name => name.startsWith('.kpbot-dsh-stage-'));
  assert.equal(stages.length, 1);
  assert.match(await fs.readFile(path.join(config, stages[0], 'old/retired/SKILL.md'), 'utf8'), /name: retired/);
});

test('DSH refuses a user directory that appears while staging a new skill', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  await skill(directory);
  await installSkills(options);
  await skill(directory, 'another', 'another');
  const original = await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8');
  const copy = fs.cp;
  const injected = context.mock.method(fs, 'cp', async (...argumentsList) => {
    await copy(...argumentsList);
    await put(path.join(config, 'skills/another/user.txt'), 'User content');
  });
  try { await assert.rejects(installSkills(options), /Unmanaged skill directory conflict/); }
  finally { injected.mock.restore(); }
  assert.equal(await fs.readFile(path.join(config, 'skills/another/user.txt'), 'utf8'), 'User content');
  assert.equal(await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8'), original);
});

test('DSH refuses installation state that changes during preparation', async context => {
  const { root, options, config } = await fixture(context);
  await skill(await plugin(root));
  await installSkills(options);
  const original = await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8');
  const manifestPath = path.join(config, 'kpbot-manifest.json');
  const changed = { ...JSON.parse(await fs.readFile(manifestPath, 'utf8')), externalRevision: 'changed' };
  const copy = fs.cp;
  const injected = context.mock.method(fs, 'cp', async (...argumentsList) => {
    await copy(...argumentsList);
    await json(manifestPath, changed);
  });
  try { await assert.rejects(installSkills(options), /changed during preparation/); }
  finally { injected.mock.restore(); }
  assert.deepEqual(JSON.parse(await fs.readFile(manifestPath, 'utf8')), changed);
  assert.equal(await fs.readFile(path.join(config, 'skills/example/SKILL.md'), 'utf8'), original);
});

for (const mutation of ['changed', 'missing']) {
  test(`DSH preserves retired skills when ownership is ${mutation} during preparation`, async context => {
    const { root, options, config } = await fixture(context);
    const directory = await plugin(root);
    await skill(directory);
    await skill(directory, 'retired', 'retired');
    await installSkills(options);
    await fs.rm(path.join(directory, 'skills/retired'), { recursive: true });
    const marker = path.join(config, 'skills/retired/.kpbot-dsh-owner.json');
    const copy = fs.cp;
    let expected;
    const injected = context.mock.method(fs, 'cp', async (...argumentsList) => {
      await copy(...argumentsList);
      if (mutation === 'changed') await json(marker, { owner: 'user', skillDir: 'retired' });
      else await fs.unlink(marker);
      expected = await snapshot(path.join(config, 'skills'));
    });
    const manifest = await fs.readFile(path.join(config, 'kpbot-manifest.json'));
    try { await assert.rejects(installSkills(options), mutation === 'changed' ? /ownership mismatch/ : /ENOENT/); }
    finally { injected.mock.restore(); }
    assert.deepEqual(await snapshot(path.join(config, 'skills')), expected);
    assert.deepEqual(await fs.readFile(path.join(config, 'kpbot-manifest.json')), manifest);
    assert.deepEqual((await fs.readdir(config)).filter(name => name.startsWith('.kpbot-dsh-stage-')), []);
  });
}

for (const phase of ['remove', 'manifest']) {
  test(`failed DSH uninstall ${phase} retains its manifest and supports retry`, async context => {
    const { root, options, config } = await fixture(context);
    const directory = await plugin(root);
    await skill(directory);
    await skill(directory, 'another', 'another');
    await installSkills(options);
    await put(path.join(config, 'skills/user/SKILL.md'), 'User content');
    const method = phase === 'remove' ? 'rm' : 'unlink';
    const operation = fs[method];
    const injected = context.mock.method(fs, method, async (target, ...argumentsList) => {
      if (target === path.join(config, phase === 'remove' ? 'skills/example' : 'kpbot-manifest.json')) {
        throw new Error('Fixture uninstall failure');
      }
      return operation(target, ...argumentsList);
    });
    try { await assert.rejects(uninstallSkills(options), /Fixture uninstall failure/); }
    finally { injected.mock.restore(); }
    assert.equal(await exists(path.join(config, 'kpbot-manifest.json')), true);
    await uninstallSkills(options);
    for (const name of ['another', 'example']) assert.equal(await exists(path.join(config, 'skills', name)), false);
    assert.equal(await fs.readFile(path.join(config, 'skills/user/SKILL.md'), 'utf8'), 'User content');
    assert.deepEqual((await uninstallSkills(options)).removed, []);
  });
}

test('DSH skills named new and old cannot collide with transaction directories', async context => {
  const { root, options, config } = await fixture(context);
  const directory = await plugin(root);
  for (const name of ['new', 'old']) await skill(directory, name, name);
  await installSkills(options);
  await installSkills(options);
  for (const name of ['new', 'old']) {
    assert.match(await fs.readFile(path.join(config, 'skills', name, 'SKILL.md'), 'utf8'), new RegExp(`name: ${name}`));
  }
  await uninstallSkills(options);
  for (const name of ['new', 'old']) assert.equal(await exists(path.join(config, 'skills', name)), false);
});

for (const reason of ['missing-plugins', 'unsupported-platform', 'all-excluded']) {
  for (const previouslyInstalled of [false, true]) {
    test(`empty DSH catalog (${reason}, installed=${previouslyInstalled}) cannot publish or remove skills`, async context => {
      const { root, options, config } = await fixture(context);
      const directory = await plugin(root);
      await skill(directory);
      if (previouslyInstalled) {
        await installSkills(options);
        await put(path.join(config, 'skills/user/notes.txt'), 'User content');
      }
      const before = previouslyInstalled ? await snapshot(config) : null;
      if (reason === 'missing-plugins') await fs.rm(path.join(root, 'Plugins'), { recursive: true });
      else if (reason === 'unsupported-platform') {
        await json(path.join(directory, '.claude-plugin/plugin.json'), { platforms: ['claude'] });
      } else {
        await json(path.join(directory, '.claude-plugin/plugin.json'), { platforms: ['dsh'], dsh: { exclude: ['example'] } });
      }
      assert.deepEqual(await discoverSkills(root), []);
      await assert.rejects(installSkills(options), /No plugins declare DSH skills/);
      if (previouslyInstalled) assert.deepEqual(await snapshot(config), before);
      else assert.equal(await exists(config), false);
    });
  }
}

for (const location of ['skill', 'skills-parent', 'overlay', 'common/scripts', 'unsupported/resources', 'new-resource']) {
  test(`DSH refuses destinations overlapping ${location} resources before any copy`, async context => {
    const { root } = await fixture(context);
    const directory = await plugin(root);
    const source = await skill(directory);
    const overlay = path.join(directory, 'dsh/example');
    await put(path.join(overlay, 'SKILL.md'), markdown());
    const installPath = location === 'skill' ? source : location === 'skills-parent' ? path.dirname(source)
      : location === 'overlay' ? overlay : path.join(root, 'Plugins', location);
    if (location !== 'overlay') await put(path.join(installPath, 'preserve.txt'), 'Source resource');
    const options = { repositoryRoot: root, installPath };
    const before = await snapshot(root);
    const injected = context.mock.method(fs, 'cp', async () => { throw new Error('Unexpected recursive copy'); });
    try {
      await assert.rejects(installSkills(options), /overlaps skill resources/);
      assert.equal(injected.mock.callCount(), 0);
    } finally { injected.mock.restore(); }
    assert.equal(await exists(configRoot(options)), false);
    assert.deepEqual(await snapshot(root), before);
  });
}

test('global DSH home inside common scripts is rejected before any copy', async context => {
  const { root } = await fixture(context);
  await skill(await plugin(root));
  const installPath = path.join(root, 'Plugins/common/scripts/new-dsh-home');
  await put(path.join(root, 'Plugins/common/scripts/preserve.sh'), 'Shared source');
  const before = await snapshot(root);
  const injected = context.mock.method(fs, 'cp', async () => { throw new Error('Unexpected recursive copy'); });
  try {
    await assert.rejects(installSkills({ repositoryRoot: root, level: 'global', installPath }), /overlaps skill resources/);
    assert.equal(injected.mock.callCount(), 0);
  } finally { injected.mock.restore(); }
  assert.equal(await exists(installPath), false);
  assert.deepEqual(await snapshot(root), before);
});

test('DSH may install at the repository project root without copying into its sources', async context => {
  const { root } = await fixture(context);
  await skill(await plugin(root));
  const options = { repositoryRoot: root, installPath: root };
  assert.deepEqual((await installSkills(options)).installed, ['example']);
  assert.equal(await exists(path.join(root, '.dsh/skills/example/SKILL.md')), true);
});

test('collisions and unmanaged directories abort before any write', async t => {
  const { root, options, config } = await fixture(t);
  await skill(await plugin(root, 'a'), 'one', 'duplicate');
  await skill(await plugin(root, 'b'), 'two', 'duplicate');
  await assert.rejects(installSkills(options), /Duplicate skill name/);
  assert.equal(await exists(config), false);

  const second = await fixture(t);
  await skill(await plugin(second.root));
  await put(path.join(second.config, 'skills/example/user.txt'), 'keep');
  await assert.rejects(installSkills(second.options), /Unmanaged skill directory conflict/);
  assert.equal(await fs.readFile(path.join(second.config, 'skills/example/user.txt'), 'utf8'), 'keep');
  assert.equal(await exists(path.join(second.config, 'kpbot-manifest.json')), false);
});

test('global and project targets follow HOME, DSH_HOME and paths with spaces', async t => {
  const { root, options } = await fixture(t);
  await skill(await plugin(root));
  const home = path.join(root, 'user home');
  const dshHome = path.join(root, 'custom dsh home');
  assert.equal(configRoot({ cwd: home }), path.join(home, '.dsh'));
  assert.equal(configRoot({ level: 'global', env: { HOME: home } }), path.join(home, '.dsh'));
  assert.equal(configRoot({ level: 'global', env: { HOME: home, DSH_HOME: dshHome } }), dshHome);

  const globalOptions = { repositoryRoot: root, level: 'global', env: { HOME: home, DSH_HOME: dshHome } };
  await installSkills(globalOptions);
  assert.equal(await exists(path.join(dshHome, 'skills/example/SKILL.md')), true);
  await uninstallSkills(globalOptions);
  assert.equal(await exists(path.join(dshHome, 'skills/example')), false);
});

// --- Fail closed on untrusted input -----------------------------------------

test('unsafe names, links and metadata abort before any write', async t => {
  const excluded = await fixture(t);
  await skill(await plugin(excluded.root, 'a', { platforms: ['dsh'], dsh: { exclude: ['../user'] } }));
  await assert.rejects(installSkills(excluded.options), /Unsafe skill directory name/);
  assert.equal(await exists(excluded.config), false);

  const linked = await fixture(t);
  const directory = await plugin(linked.root, 'a');
  const base = await skill(directory);
  const outside = path.join(linked.root, 'outside');
  await put(path.join(outside, 'secret'), 'keep');
  await linkDirectory(outside, path.join(base, 'references'));
  await assert.rejects(installSkills(linked.options), /Symlink not allowed/);
  assert.equal(await fs.readFile(path.join(outside, 'secret'), 'utf8'), 'keep');

  const metadata = await fixture(t);
  const broken = await plugin(metadata.root);
  await skill(broken);
  await put(path.join(broken, '.claude-plugin/plugin.json'), '{"platforms":["dsh"],"dsh":null}');
  await assert.rejects(installSkills(metadata.options), /Invalid/);

  const frontmatter = await fixture(t);
  const invalid = await skill(await plugin(frontmatter.root));
  await put(path.join(invalid, 'SKILL.md'), markdown('../escape'));
  await assert.rejects(installSkills(frontmatter.options), /skill name/i);
  assert.equal(await exists(frontmatter.config), false);
});

test('tampered manifests and lost ownership markers block every deletion', async t => {
  const { root, options, config } = await fixture(t);
  await skill(await plugin(root));
  await installSkills(options);
  await put(path.join(config, 'skills/user/user.txt'), 'keep');
  const manifestPath = path.join(config, 'kpbot-manifest.json');
  const original = await fs.readFile(manifestPath);

  const tampered = JSON.parse(original);
  tampered.installed_skills.push('../user');
  await json(manifestPath, tampered);
  await assert.rejects(uninstallSkills(options));
  assert.equal(await fs.readFile(path.join(config, 'skills/user/user.txt'), 'utf8'), 'keep');

  await fs.writeFile(manifestPath, original);
  await fs.unlink(path.join(config, 'skills/example/.kpbot-dsh-owner.json'));
  await assert.rejects(uninstallSkills(options));
  assert.equal(await exists(path.join(config, 'skills/example/SKILL.md')), true);
});

// --- Existing platforms must not regress ------------------------------------

test('a legacy relative directory named dsh still installs and uninstalls for claude/opencode', async t => {
  for (const platform of ['claude', 'opencode']) {
    const { root } = await shellFixture(t);
    await skill(await plugin(root, 'minimal', { platforms: [platform] }));
    const env = { ...process.env, HOME: path.join(root, 'home') };
    const install = spawnSync(bash, [shellPath(path.join(root, 'install.sh')), 'project', platform, 'dsh'],
      { cwd: root, env, encoding: 'utf8' });
    assert.equal(install.status, 0, `${install.stdout}\n${install.stderr}`);
    const config = path.join(root, 'dsh', `.${platform}`);
    assert.equal(await exists(path.join(config, 'skills/example/SKILL.md')), true);
    assert.equal(await exists(path.join(root, 'dsh/.dsh')), false);

    const uninstall = spawnSync(bash, [shellPath(path.join(root, 'bin/_uninstall.sh')), 'project', platform, 'dsh'],
      { cwd: root, env, encoding: 'utf8' });
    assert.equal(uninstall.status, 0, `${uninstall.stdout}\n${uninstall.stderr}`);
    assert.equal(await exists(path.join(config, 'skills/example')), false);
  }
});

test('shell CLI installs and uninstalls dsh at project and global scope', async t => {
  const { root, project } = await shellFixture(t);
  await skill(await plugin(root));
  const home = path.join(root, 'user home');
  const dshHome = path.join(root, 'DSH home');
  await put(path.join(home, '.claude/kpbot-manifest.json'), 'Claude sentinel');
  const env = { ...process.env, HOME: home, DSH_HOME: dshHome };
  const run = args => {
    const result = spawnSync(bash, [shellPath(path.join(root, 'bin/kpbot')), ...args], { env, cwd: root, encoding: 'utf8' });
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  };
  run(['install', 'project', 'dsh', project]);
  assert.equal(await exists(path.join(project, '.dsh/skills/example/SKILL.md')), true);
  run(['uninstall', 'project', 'dsh', project]);
  assert.equal(await exists(path.join(project, '.dsh/skills/example')), false);

  run(['install', 'global', 'dsh']);
  assert.equal(await exists(path.join(dshHome, 'skills/example/SKILL.md')), true);
  run(['uninstall', 'global', 'dsh']);
  assert.equal(await exists(path.join(dshHome, 'skills/example')), false);

  // Omitting the level defaults to project scope.
  run(['install', 'dsh', project]);
  assert.equal(await exists(path.join(project, '.dsh/skills/example/SKILL.md')), true);
  run(['uninstall', 'dsh', project]);
  assert.equal(await exists(path.join(home, '.claude/kpbot-manifest.json')), true);
});

test('CLI rejects unknown platforms and ambiguous arguments', async () => {
  await assert.rejects(main(['install', 'project', 'unknown']), /Unknown platform/);
  await assert.rejects(main(['uninstall', 'project', 'claude']), /Unknown platform/);
  await assert.rejects(main(['install', 'project', 'dsh', '/a', '/b']), /Usage/);
});
