import fs from 'node:fs/promises';
import path from 'node:path';

const statOptions = { bigint: true };
const identity = stat => `${stat.dev}:${stat.ino}`;

async function existing(file) {
  try { return await fs.lstat(file, statOptions); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function sameFile(a, b) {
  return a && b && identity(a) === identity(b) && a.size === b.size && a.mtimeNs === b.mtimeNs;
}

function unchanged(a, b) {
  return a === b || (sameFile(a, b) && a.ctimeNs === b.ctimeNs);
}

export async function planOutputs(input, files, force) {
  const source = await fs.stat(input, statOptions);
  const seen = new Map([[identity(source), input]]);
  const entries = [];
  for (const file of files) {
    const original = await existing(file);
    if (original) {
      if (!original.isFile()) throw new Error(`Output must be a regular file, not a link or directory: ${file}`);
      const alias = seen.get(identity(original));
      if (alias) throw new Error(`Output aliases another file: ${file} and ${alias}; choose another output stem`);
      if (!force) throw new Error(`Output exists: ${file}; use another filename or --force for an authorized update`);
      seen.set(identity(original), file);
    }
    entries.push({ file, original });
  }
  return { input, source, entries };
}

async function placeExclusive(from, to) {
  try { await fs.link(from, to); return; }
  catch (error) {
    if (!['EPERM', 'EACCES', 'ENOTSUP', 'EOPNOTSUPP', 'ENOSYS', 'EMLINK', 'EXDEV'].includes(error.code)) throw error;
  }
  // Filesystems without hard links still support exclusive creation and same-directory renames.
  const handle = await fs.open(to, 'wx', 0o600);
  let placeholder;
  try {
    try { placeholder = await handle.stat(statOptions); }
    finally { await handle.close(); }
    if (!unchanged(placeholder, await existing(to))) throw new Error(`Output changed during reservation: ${to}`);
    await fs.rename(from, to);
  } catch (error) {
    if (placeholder && unchanged(placeholder, await existing(to))) await fs.unlink(to);
    throw error;
  }
}

async function restore(entry) {
  if (entry.published) {
    const current = await existing(entry.file);
    if (current) {
      if (!sameFile(current, entry.staged)) throw new Error(`Output changed: ${entry.file}`);
      await fs.unlink(entry.file);
    }
  }
  if (entry.saved) await placeExclusive(entry.backup, entry.file);
}

export async function publishOutputs({ input, source, entries }, data) {
  const directory = path.dirname(entries[0].file);
  const stem = path.basename(entries[0].file, '.excalidraw');
  const stage = path.join(directory, `.${stem}.lock`);
  await fs.mkdir(directory, { recursive: true });
  try { await fs.mkdir(stage, { mode: 0o700 }); }
  catch (error) {
    if (error.code === 'EEXIST') throw new Error(`Output set is busy or needs recovery: ${stage}`);
    throw error;
  }
  let keepBackups = false;
  const batch = entries.map((entry, i) => ({
    ...entry, temporary: path.join(stage, `new-${i}`), backup: path.join(stage, `old-${i}`),
  }));
  try {
    for (const [i, entry] of batch.entries()) {
      await fs.writeFile(entry.temporary, data[i], { flag: 'wx' });
      if (entry.original) await fs.chmod(entry.temporary, Number(entry.original.mode & 0o777n));
      entry.staged = await fs.lstat(entry.temporary, statOptions);
    }
    if (!unchanged(source, await fs.stat(input, statOptions))) throw new Error(`Input changed during export: ${input}`);
    for (const entry of batch) {
      if (!unchanged(entry.original, await existing(entry.file))) throw new Error(`Output changed during export: ${entry.file}`);
      if (entry.original) {
        await fs.rename(entry.file, entry.backup);
        entry.saved = true;
        if (!sameFile(entry.original, await existing(entry.backup))) throw new Error(`Output changed during replacement: ${entry.file}`);
      }
      await placeExclusive(entry.temporary, entry.file);
      entry.published = true;
    }
  } catch (error) {
    const failures = [];
    for (const entry of batch.reverse()) {
      try { await restore(entry); } catch (failure) { failures.push(failure.message); }
    }
    if (failures.length) {
      keepBackups = true;
      throw new Error(`${error.message}; rollback incomplete, recovery files at ${stage}: ${failures.join('; ')}`);
    }
    throw error;
  } finally {
    if (!keepBackups) await fs.rm(stage, { recursive: true });
  }
}
