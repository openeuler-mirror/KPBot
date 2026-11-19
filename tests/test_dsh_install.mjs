import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { discoverSkills, readSkill } from '../adapters/dsh/catalog.mjs';
import { installSkills, uninstallSkills, configRoot, main } from '../adapters/dsh/install.mjs';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const markdown = (name = 'example', extra = '', body = 'Skill body.') => `---\nname: ${name}\ndescription: >\n  A folded\n  description.\n${extra}---\n${body}\n`;
async function put(file, text) { await fs.mkdir(path.dirname(file), { recursive: true }); await fs.writeFile(file, text); }
async function json(file, data) { await put(file, JSON.stringify(data)); }
async function exists(file) { try { await fs.lstat(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } }
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
  await fs.symlink(path.join(repository, 'node_modules'), path.join(result.root, 'node_modules'));
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

test('installs only packages that name dsh, and never scans inside a package', async t => {
  const { root } = await fixture(t);
  for (const [index, metadata] of [{}, { platforms: ['unknown'] }, { platforms: ['claude', 'opencode'] }].entries()) {
    await skill(await plugin(root, `package-${index}`, metadata));
  }
  const unsupported = await plugin(root, 'unsupported', { platforms: ['claude'] });
  // Resource trees of unsupported packages may contain links; discovery must not enter them.
  await fs.symlink('/does/not/exist', path.join(unsupported, 'vendored-resource'));
  assert.deepEqual(await discoverSkills(root), []);
});

// --- Install contract --------------------------------------------------------

test('overlay replaces only the instruction file and keeps canonical resources', async t => {
  const { root, options, config } = await fixture(t);
  const directory = await plugin(root, 'domain/package', { platforms: ['dsh'], dsh: { exclude: ['skip'] } });
  const base = await skill(directory, 'kunpeng_microarch', 'kunpeng-microarch');
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
  await fs.symlink(outside, path.join(base, 'references'));
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
    const install = spawnSync('bash', [path.join(root, 'install.sh'), 'project', platform, 'dsh'],
      { cwd: root, env, encoding: 'utf8' });
    assert.equal(install.status, 0, `${install.stdout}\n${install.stderr}`);
    const config = path.join(root, 'dsh', `.${platform}`);
    assert.equal(await exists(path.join(config, 'skills/example/SKILL.md')), true);
    assert.equal(await exists(path.join(root, 'dsh/.dsh')), false);

    const uninstall = spawnSync('bash', [path.join(root, 'bin/_uninstall.sh'), 'project', platform, 'dsh'],
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
    const result = spawnSync('bash', [path.join(root, 'bin/kpbot'), ...args], { env, cwd: root, encoding: 'utf8' });
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
