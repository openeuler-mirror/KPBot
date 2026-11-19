import fs from 'node:fs/promises';
import path from 'node:path';
import { parseDocument } from 'yaml';

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

// Directory names are kept verbatim (not the frontmatter name), but never used
// as paths until validated. This also applies to untrusted install manifests.
export function validateDirectoryName(name) {
  if (typeof name !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name)) {
    throw new Error(`Unsafe skill directory name: ${JSON.stringify(name)}`);
  }
  return name;
}

export async function lstatOptional(target) {
  try { return await fs.lstat(target); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

// Do not follow links, even internal links: copying dereferenced links can turn
// an otherwise safe resource tree into a write outside the destination later.
export async function assertSafeTree(target) {
  const stat = await fs.lstat(target);
  if (stat.isSymbolicLink()) throw new Error(`Symlink not allowed: ${target}`);
  if (stat.isDirectory()) {
    for (const name of (await fs.readdir(target)).sort()) await assertSafeTree(path.join(target, name));
  } else if (!stat.isFile()) {
    throw new Error(`Unsupported file type: ${target}`);
  }
}

async function checkedPath(root, target) {
  const relative = path.relative(root, path.resolve(target));
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error(`Path escapes repository: ${target}`);
  }
  let current = root;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if ((await fs.lstat(current)).isSymbolicLink()) throw new Error(`Symlink not allowed: ${current}`);
  }
  return current;
}

function parseMarkdown(text, source, required = true) {
  text = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  const match = /^---\n([\s\S]*?)\n---(?:\n|$)/.exec(text);
  if (!match) {
    if (required) throw new Error(`Missing or malformed frontmatter: ${source}`);
    return { data: {}, body: text.trim() };
  }

  const document = parseDocument(match[1], { uniqueKeys: true });
  if (document.errors.length) throw new Error(`Invalid frontmatter in ${source}: ${document.errors[0].message}`);
  const data = document.toJS({ maxAliasCount: 100 });
  if (!isObject(data)) throw new Error(`Frontmatter must be a mapping: ${source}`);
  return { data, body: text.slice(match[0].length).trim() };
}

export function parseSkill(text, source) {
  const parsed = parseMarkdown(text, source);
  const { data } = parsed;
  if (typeof data.name !== 'string' || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(data.name)) {
    throw new Error(`Invalid skill name in ${source}`);
  }
  if (typeof data.description !== 'string' || !data.description.trim()) {
    throw new Error(`Invalid skill description in ${source}`);
  }
  for (const flag of ['disable-model-invocation', 'user-invocable', 'model-invocable']) {
    if (flag in data && typeof data[flag] !== 'boolean') throw new Error(`Invalid ${flag} in ${source}: expected boolean`);
  }
  return {
    ...parsed,
    name: data.name,
    description: data.description.trim(),
    invocation: {
      modelInvocable: data['disable-model-invocation'] !== true && data['model-invocable'] !== false,
      userInvocable: data['user-invocable'] !== false,
    },
  };
}

async function pluginDirectories(pluginsRoot) {
  const result = [];
  async function visit(directory) {
    const stat = await fs.lstat(directory);
    if (stat.isSymbolicLink()) throw new Error(`Symlink not allowed: ${directory}`);
    if (!stat.isDirectory()) return;
    const metadata = path.join(directory, '.claude-plugin', 'plugin.json');
    // A manifest marks a package boundary. Its skills, cases and vendored
    // dependencies are resources, not plugin grouping directories.
    if (await lstatOptional(metadata)) { result.push(directory); return; }
    for (const child of (await fs.readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (child.isSymbolicLink()) throw new Error(`Symlink not allowed: ${path.join(directory, child.name)}`);
      if (child.isDirectory()) await visit(path.join(directory, child.name));
    }
  }
  if (await lstatOptional(pluginsRoot)) await visit(pluginsRoot);
  return result;
}

/** Discover explicitly opted-in packages at any depth below Plugins/. */
export async function discoverSkills(repositoryRoot) {
  const root = await fs.realpath(repositoryRoot);
  const entries = [];
  const names = new Set();
  const directories = new Set();
  for (const pluginRoot of await pluginDirectories(path.join(root, 'Plugins'))) {
    const metadataPath = await checkedPath(root, path.join(pluginRoot, '.claude-plugin', 'plugin.json'));
    let metadata;
    try { metadata = JSON.parse(await fs.readFile(metadataPath, 'utf8')); }
    catch (error) { throw new Error(`Invalid plugin metadata ${metadataPath}: ${error.message}`); }
    if (!isObject(metadata)) throw new Error(`Invalid plugin metadata: ${metadataPath}`);
    if (metadata.platforms !== undefined && (!Array.isArray(metadata.platforms) || metadata.platforms.some(value => typeof value !== 'string'))) {
      throw new Error(`Invalid platforms in ${metadataPath}`);
    }
    if (!metadata.platforms?.includes('dsh')) continue;
    if (metadata.dsh !== undefined && !isObject(metadata.dsh)) throw new Error(`Invalid dsh configuration: ${metadataPath}`);
    const excludes = metadata.dsh?.exclude === undefined ? [] : metadata.dsh.exclude;
    if (!Array.isArray(excludes)) throw new Error(`Invalid dsh.exclude: ${metadataPath}`);
    excludes.forEach(validateDirectoryName);
    const skillsRoot = path.join(pluginRoot, 'skills');
    if (!(await lstatOptional(skillsRoot))) continue;
    await checkedPath(root, skillsRoot);
    const overlayRoot = path.join(pluginRoot, 'dsh');
    if (await lstatOptional(overlayRoot)) await checkedPath(root, overlayRoot);
    for (const child of (await fs.readdir(skillsRoot, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      if (child.isSymbolicLink()) throw new Error(`Symlink not allowed: ${path.join(skillsRoot, child.name)}`);
      if (!child.isDirectory()) continue;
      const skillDir = validateDirectoryName(child.name);
      if (excludes.includes(skillDir)) continue;
      const resourceDir = await checkedPath(root, path.join(skillsRoot, skillDir));
      const base = path.join(resourceDir, 'SKILL.md');
      if (!(await lstatOptional(base))) throw new Error(`Missing SKILL.md: ${resourceDir}`);
      const overlayDirectory = path.join(overlayRoot, skillDir);
      if (await lstatOptional(overlayDirectory)) {
        await checkedPath(root, overlayDirectory);
        for (const file of await fs.readdir(overlayDirectory, { withFileTypes: true })) {
          if (file.isSymbolicLink()) throw new Error(`Symlink not allowed: ${path.join(overlayDirectory, file.name)}`);
          if (file.name !== 'SKILL.md' || !file.isFile()) {
            throw new Error(`Only SKILL.md overlays are supported: ${overlayDirectory}`);
          }
        }
      }
      const overlay = path.join(overlayDirectory, 'SKILL.md');
      await checkedPath(root, base);
      const skillPath = await checkedPath(root, await lstatOptional(overlay) ? overlay : base);
      const skill = parseSkill(await fs.readFile(skillPath, 'utf8'), skillPath);
      if (names.has(skill.name)) throw new Error(`Duplicate skill name: ${skill.name}`);
      if (directories.has(skillDir)) throw new Error(`Duplicate skill directory: ${skillDir}`);
      names.add(skill.name);
      directories.add(skillDir);
      entries.push({ name: skill.name, description: skill.description, invocation: skill.invocation, path: skillPath, resourceDir, skillDir, pluginRoot });
    }
  }
  return entries;
}

/** Read body and invocation semantics shared by native and installed skills. */
export async function readSkill(entry, repositoryRoot) {
  const root = await fs.realpath(repositoryRoot);
  const source = await checkedPath(root, entry.path);
  const runtime = await checkedPath(root, path.join(root, 'adapters', 'dsh', 'runtime.md'));
  const skill = parseSkill(await fs.readFile(source, 'utf8'), source);
  const runtimeBody = parseMarkdown(await fs.readFile(runtime, 'utf8'), runtime, false).body;
  return {
    content: [runtimeBody, skill.body].filter(Boolean).join('\n\n'),
    name: skill.name,
    description: skill.description,
    invocation: skill.invocation,
  };
}
