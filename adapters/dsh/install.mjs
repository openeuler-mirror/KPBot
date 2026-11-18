#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { discoverSkills, readSkill, assertSafeTree, lstatOptional, validateDirectoryName } from './catalog.mjs';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const MANIFEST = 'kpbot-manifest.json';
const OWNER = '.kpbot-dsh-owner.json';

export function configRoot({ level = 'project', installPath, cwd = process.cwd(), env = process.env } = {}) {
  if (!['project', 'global'].includes(level)) throw new Error(`Unknown installation level: ${level}`);
  if (level === 'project') return path.resolve(installPath || cwd, '.dsh');
  // A supplied global path is the DSH home, just like DSH_HOME.
  return path.resolve(installPath || env.DSH_HOME || path.join(env.HOME || os.homedir(), '.dsh'));
}

async function checkAncestors(target) {
  const absolute = path.resolve(target);
  let current = path.parse(absolute).root;
  for (const component of absolute.slice(current.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    const stat = await lstatOptional(current);
    if (stat?.isSymbolicLink()) throw new Error(`Symlink not allowed: ${current}`);
    if (stat && !stat.isDirectory()) throw new Error(`Expected directory: ${current}`);
  }
}

async function readJson(target) {
  const stat = await fs.lstat(target);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`Expected regular metadata file: ${target}`);
  try { return JSON.parse(await fs.readFile(target, 'utf8')); }
  catch (error) { throw new Error(`Invalid metadata ${target}: ${error.message}`); }
}

async function managedState(root) {
  await checkAncestors(path.join(root, 'skills'));
  const manifestPath = path.join(root, MANIFEST);
  if (!(await lstatOptional(manifestPath))) return null;
  const manifest = await readJson(manifestPath);
  if (manifest?.schemaVersion !== 1 || manifest.brand !== 'KPBot' || manifest.tool !== 'dsh' ||
      typeof manifest.owner !== 'string' || !/^[0-9a-f-]{36}$/.test(manifest.owner) ||
      !Array.isArray(manifest.installed_skills)) {
    throw new Error(`Invalid DSH installation manifest: ${manifestPath}`);
  }
  const names = manifest.installed_skills;
  names.forEach(validateDirectoryName);
  if (new Set(names).size !== names.length) throw new Error(`Duplicate directories in manifest: ${manifestPath}`);
  // Validate every deletion candidate before performing any writes/deletions.
  // A manifest alone cannot claim a pre-existing user directory.
  for (const name of names) {
    const target = path.join(root, 'skills', name);
    const stat = await lstatOptional(target);
    if (!stat) continue;
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`Unsafe managed directory: ${target}`);
    await assertSafeTree(target);
    const marker = await readJson(path.join(target, OWNER));
    if (marker?.owner !== manifest.owner || marker.skillDir !== name) {
      throw new Error(`Skill ownership mismatch: ${target}`);
    }
  }
  return manifest;
}

async function copySharedScript(repositoryRoot, destination, content) {
  if (!content.includes('print_logo.sh')) return;
  const source = path.join(repositoryRoot, 'Plugins', 'common', 'scripts', 'print_logo.sh');
  if (!(await lstatOptional(source))) throw new Error(`Missing shared script: ${source}`);
  // Check all source ancestors as well as the file itself.
  await checkAncestors(path.dirname(source));
  await assertSafeTree(source);
  const target = path.join(destination, 'scripts', 'print_logo.sh');
  if (!(await lstatOptional(target))) {
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.copyFile(source, target);
  }
  await fs.chmod(target, 0o755);
}

export async function installSkills({ repositoryRoot = repository, ...options } = {}) {
  const entries = await discoverSkills(repositoryRoot);
  const prepared = await Promise.all(entries.map(async entry => {
    await assertSafeTree(entry.resourceDir);
    const overlay = path.join(entry.pluginRoot, 'dsh', entry.skillDir);
    if (await lstatOptional(overlay)) await assertSafeTree(overlay);
    return { entry, skill: await readSkill(entry, repositoryRoot) };
  }));
  const root = configRoot(options);
  const previous = await managedState(root);
  const previousNames = new Set(previous?.installed_skills || []);
  for (const { entry } of prepared) {
    const target = path.join(root, 'skills', entry.skillDir);
    if ((await lstatOptional(target)) && !previousNames.has(entry.skillDir)) {
      throw new Error(`Unmanaged skill directory conflict: ${target}`);
    }
  }
  // Build the complete result before replacing a managed directory. Discovery,
  // metadata, ownership and collision failures above have no installation writes.
  await fs.mkdir(root, { recursive: true });
  const stage = await fs.mkdtemp(path.join(root, '.kpbot-dsh-stage-'));
  const owner = previous?.owner || randomUUID();
  try {
    for (const { entry, skill } of prepared) {
      const target = path.join(stage, entry.skillDir);
      await fs.cp(entry.resourceDir, target, { recursive: true });
      const overlay = path.join(entry.pluginRoot, 'dsh', entry.skillDir);
      if (await lstatOptional(overlay)) await fs.cp(overlay, target, { recursive: true, force: true });
      const original = (await fs.readFile(entry.path, 'utf8')).replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
      const header = /^---\n[\s\S]*?\n---(?:\n|$)/.exec(original)[0].trimEnd();
      await fs.writeFile(path.join(target, 'SKILL.md'), `${header}\n\n${skill.content}\n`);
      await copySharedScript(repositoryRoot, target, skill.content);
      await fs.writeFile(path.join(target, OWNER), JSON.stringify({ owner, skillDir: entry.skillDir }) + '\n');
    }
    await fs.mkdir(path.join(root, 'skills'), { recursive: true });
    // Revalidate immediately before committing: never trust manifest path strings.
    await managedState(root);
    for (const name of previousNames) await fs.rm(path.join(root, 'skills', name), { recursive: true, force: true });
    for (const { entry } of prepared) await fs.rename(path.join(stage, entry.skillDir), path.join(root, 'skills', entry.skillDir));
    const manifest = { schemaVersion: 1, brand: 'KPBot', tool: 'dsh', owner, installed_skills: entries.map(entry => entry.skillDir) };
    const stagedManifest = path.join(stage, MANIFEST);
    await fs.writeFile(stagedManifest, JSON.stringify(manifest, null, 2) + '\n');
    await fs.rename(stagedManifest, path.join(root, MANIFEST));
    return { root, installed: manifest.installed_skills };
  } finally {
    // stage is created by mkdtemp under the validated root, never from a manifest.
    await fs.rm(stage, { recursive: true, force: true });
  }
}

export async function uninstallSkills(options = {}) {
  const root = configRoot(options);
  const manifest = await managedState(root);
  if (!manifest) return { root, removed: [] };
  for (const name of manifest.installed_skills) {
    await fs.rm(path.join(root, 'skills', name), { recursive: true, force: true });
  }
  await fs.unlink(path.join(root, MANIFEST));
  return { root, removed: manifest.installed_skills };
}

const HELP = `Usage: node adapters/dsh/install.mjs <install|uninstall> [project|global] [dsh] [path]
  project path: project root (default: current directory), installs .dsh/skills
  global path: DSH home (default: DSH_HOME or ~/.dsh), installs skills/
Examples:
  kpbot install project dsh "/path/with spaces"
  kpbot install global dsh
  kpbot uninstall project dsh "/path/with spaces"`;

export async function main(args = process.argv.slice(2)) {
  if (args.includes('--help') || args.includes('-h')) { console.log(HELP); return; }
  const [command, ...rest] = args;
  if (!['install', 'uninstall'].includes(command)) throw new Error(HELP);
  let level = 'project';
  if (['project', 'global'].includes(rest[0])) level = rest.shift();
  const explicitPlatform = rest[0] === 'dsh';
  if (explicitPlatform) rest.shift();
  // A platform-looking token is an error, not a path or a Claude fallback.
  if (!explicitPlatform && rest[0] && !rest[0].includes('/') && !rest[0].includes('\\') && !rest[0].startsWith('.')) {
    throw new Error(`Unknown platform or ambiguous path: ${rest[0]}; use dsh followed by an absolute or ./relative path`);
  }
  if (rest.length > 1) throw new Error(HELP);
  const options = { level, installPath: rest[0] };
  const result = command === 'install' ? await installSkills(options) : await uninstallSkills(options);
  console.log(`DSH ${command}: ${(result.installed || result.removed).length} skill(s), ${result.root}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(`DSH: ${error.message}`); process.exitCode = 1; });
}
