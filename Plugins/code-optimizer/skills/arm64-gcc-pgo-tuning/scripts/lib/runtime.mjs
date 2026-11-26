import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { fail, fileHash, treeHash, sha, within, writeJSON, readJSON } from './contract.mjs';

export function inspect() {
  return {
    platform: process.platform,
    arch: process.arch,
    hostname: os.hostname(),
    cpu: os.cpus()[0]?.model,
    node: process.version,
    execution_supported: process.platform === 'linux' && process.arch === 'arm64',
  };
}
export function environmentGate(c) {
  const e = inspect();
  if (!e.execution_supported)
    fail(
      'unsupported_host',
      '实际执行需要 Linux ARM64；不能通过配置覆盖宿主检查',
      'blocked_environment',
    );
  if (e.hostname !== c.environment.hostname)
    fail(
      'wrong_host',
      `目标为 ${c.environment.hostname}，实际为 ${e.hostname}`,
      'blocked_environment',
    );
  for (const [name, file] of Object.entries(c.tools))
    if (!fs.existsSync(file) || !fs.statSync(file).isFile())
      fail('missing_tool', `${name}: ${file}`, 'blocked_environment');
  return e;
}
export function fingerprint(c) {
  const data = {};
  for (const d of c.datasets) {
    if (!fs.existsSync(d.path) || !fs.statSync(d.path).isFile())
      fail('missing_dataset', d.path, 'awaiting_input');
    const hash = fileHash(d.path);
    if (hash !== d.sha256) fail('dataset_changed', d.path, 'awaiting_input');
    data[d.id] = hash;
  }
  const executableHashes = {};
  for (const command of [c.correctness, ...c.training, ...c.validation])
    if (path.isAbsolute(command.argv[0]))
      executableHashes[command.argv[0]] = fileHash(command.argv[0]);
  return {
    contract: sha(JSON.stringify(c)),
    source: treeHash(c.source_dir),
    data,
    tools: Object.fromEntries(Object.entries(c.tools).map(([k, v]) => [k, fileHash(v)])),
    executables: executableHashes,
    implementation: treeHash(fileURLToPath(new URL('..', import.meta.url))),
    ambient_environment: sha(
      JSON.stringify(Object.entries(process.env).sort(([a], [b]) => a.localeCompare(b))),
    ),
  };
}
export function assertFingerprint(c, expected) {
  if (JSON.stringify(fingerprint(c)) !== JSON.stringify(expected))
    fail('inputs_changed', '源码、数据、工具或实验契约变化；必须建立新实验，不得沿用 profile');
}
export function assertOwned(root) {
  const real = fs.realpathSync(root);
  if (real !== path.resolve(root) || !fs.existsSync(path.join(root, '.pgo-owned.json')))
    fail('unowned_directory', '实验目录身份无效或包含符号链接');
  const owner = readJSON(path.join(root, '.pgo-owned.json'));
  if (owner.root !== real) fail('unowned_directory', '实验目录已移动，不允许继续删除/恢复');
}
export function removeOwned(root, relative) {
  assertOwned(root);
  const dest = path.resolve(root, relative);
  if (!within(root, dest) || !['build', 'profile-live'].includes(relative))
    fail('unsafe_delete', `拒绝清理 ${dest}`);
  if (fs.existsSync(dest)) {
    // 不允许通过顶层或祖先符号链接逃逸；目录内链接由 rm unlink，不跟随。
    if (fs.lstatSync(dest).isSymbolicLink() || fs.realpathSync(dest) !== dest)
      fail('unsafe_delete', '清理目标不是实际专属目录');
    fs.rmSync(dest, { recursive: true, force: false });
  }
  fs.mkdirSync(dest, { recursive: true });
}
export function files(root, suffix) {
  const result = [];
  if (!fs.existsSync(root)) return result;
  for (const d of fs.readdirSync(root, { withFileTypes: true })) {
    const p = path.join(root, d.name);
    if (d.isDirectory()) result.push(...files(p, suffix));
    else if (d.isFile() && p.endsWith(suffix)) result.push(p);
  }
  return result.sort();
}
export function run(c, state, argv, { cwd = c.source_dir, env = {}, label = 'command' } = {}) {
  const root = c.run_dir;
  if (!Array.isArray(argv) || argv.length === 0) fail('invalid_command', 'argv 必须非空');
  const elapsed = (Date.now() - state.started_ms) / 1000;
  if (elapsed >= c.resources.total_seconds) fail('budget_exhausted', '已达到总实验时限');
  const free = fs.statfsSync(root);
  if (Number(free.bavail) * Number(free.bsize) < c.resources.min_free_bytes)
    fail('disk_budget', '可用磁盘低于批准下限', 'blocked_environment');
  const index = ++state.command_index,
    log = path.join(root, 'logs', `${String(index).padStart(5, '0')}-${label}`);
  fs.mkdirSync(path.dirname(log), { recursive: true });
  const timeout =
    Math.max(1, Math.min(c.resources.timeout_seconds, c.resources.total_seconds - elapsed)) * 1000;
  const observation = {
    started_at: new Date().toISOString(),
    loadavg: os.loadavg(),
    free_memory: os.freemem(),
  };
  // 由 Linux supervisor 追踪同一 session；超时/遗留子进程整组清理。
  const supervisor = fileURLToPath(new URL('./supervisor.mjs', import.meta.url));
  const commandEnv = {
    ...process.env,
    ...c.workload.env,
    ...env,
    LC_ALL: 'C',
    CCACHE_DISABLE: '1',
  };
  for (const key of Object.keys(commandEnv))
    if (
      /^(CFLAGS|CXXFLAGS|LDFLAGS|CPPFLAGS|GCOV_PREFIX|GCOV_PREFIX_STRIP|LD_PRELOAD|LD_AUDIT|CMAKE_TOOLCHAIN_FILE)$/.test(
        key,
      )
    )
      delete commandEnv[key];
  const result = spawnSync(
    process.execPath,
    [supervisor, String(timeout), log, JSON.stringify(argv)],
    { cwd, env: commandEnv, encoding: 'utf8', maxBuffer: 1024 * 1024, timeout: timeout + 10000 },
  );
  const record = {
    argv,
    cwd,
    env: { ...c.workload.env, ...env, LC_ALL: 'C' },
    label,
    observation,
    finished_at: new Date().toISOString(),
    status: result.status,
    signal: result.signal,
    error: result.error?.message,
  };
  writeJSON(`${log}.command.json`, record);
  const stdout = fs.existsSync(`${log}.stdout`) ? fs.readFileSync(`${log}.stdout`, 'utf8') : '';
  const stderr = fs.existsSync(`${log}.stderr`) ? fs.readFileSync(`${log}.stderr`, 'utf8') : '';
  if (result.error || result.status !== 0)
    fail('command_failed', `${label} 执行失败，查看 ${log}.stderr 与 .command.json`);
  if (/profiling:.*(?:error|overwriting|mismatch)|libgcov profiling error/i.test(stderr))
    fail('profile_runtime_error', `采集错误: ${log}.stderr`);
  return { stdout, stderr, log };
}
export function expand(c, argv) {
  const values = {
    build: path.join(c.run_dir, 'build'),
    run: c.run_dir,
    source: c.source_dir,
    ...Object.fromEntries(c.datasets.map((d) => [`data:${d.id}`, d.path])),
  };
  return argv.map((s) =>
    s.replace(/\{([^}]+)\}/g, (_, key) => {
      if (!(key in values)) fail('unknown_placeholder', key);
      return values[key];
    }),
  );
}
export function artifactHashes(c) {
  const result = {};
  for (const name of c.artifacts) {
    const p = path.join(c.run_dir, 'build', name);
    if (!fs.existsSync(p)) fail('artifact_missing', p);
    if (!within(path.join(c.run_dir, 'build'), fs.realpathSync(p))) fail('artifact_escape', p);
    result[name] = fileHash(p);
  }
  return result;
}
export function checkElf64Arm(file) {
  const fd = fs.openSync(file, 'r'),
    header = Buffer.alloc(64);
  let size;
  try {
    size = fs.readSync(fd, header, 0, 64, 0);
  } finally {
    fs.closeSync(fd);
  }
  if (
    size < 64 ||
    header.subarray(0, 4).toString('hex') !== '7f454c46' ||
    header[4] !== 2 ||
    ![1, 2].includes(header[5])
  )
    fail('invalid_elf', '要求 ELF64 ARM64 产物: ' + file);
  const machine = header[5] === 1 ? header.readUInt16LE(18) : header.readUInt16BE(18);
  if (machine !== 183) fail('invalid_elf', 'ELF e_machine 不是 AArch64: ' + file);
}
