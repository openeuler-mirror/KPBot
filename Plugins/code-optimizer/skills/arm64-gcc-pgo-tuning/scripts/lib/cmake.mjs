import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { fail, readJSON, writeJSON, fileHash } from './contract.mjs';
import { run, files, removeOwned, artifactHashes, checkElf64Arm } from './runtime.mjs';
const moduleFile = fileURLToPath(new URL('../../cmake/pgo-targets.cmake', import.meta.url));
export function commandTokens(command) {
  const result = [];
  let word = '',
    quote = null,
    active = false;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (ch === '\\' && quote !== "'") {
      if (i + 1 >= command.length) fail('compile_command_parse', '尾部转义不完整');
      word += command[++i];
      active = true;
    } else if (quote) {
      if (ch === quote) quote = null;
      else word += ch;
      active = true;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      active = true;
    } else if (/\s/.test(ch)) {
      if (active) {
        result.push(word);
        word = '';
        active = false;
      }
    } else {
      word += ch;
      active = true;
    }
  }
  if (quote) fail('compile_command_parse', '编译命令引号不完整');
  if (active) result.push(word);
  return result;
}
export function normalizeCommand(command) {
  return commandTokens(command).filter(
    (x) =>
      !/^-(?:fprofile-(?:generate|use|update)=|Werror=(?:missing-profile|coverage-mismatch)$)/.test(
        x,
      ),
  );
}
export function mapContainsArchive(text, build, archive) {
  const expected = path.resolve(build, archive);
  return text.split('\n').some((line) => {
    const trimmed = line.trim();
    const candidate = trimmed.startsWith('LOAD ')
      ? trimmed.slice(5).trim()
      : trimmed.includes('(')
        ? trimmed.slice(0, trimmed.indexOf('(')).trim()
        : '';
    return candidate !== '' && path.resolve(build, candidate) === expected;
  });
}
export function configure(c, state, phase) {
  removeOwned(c.run_dir, 'build');
  const build = path.join(c.run_dir, 'build');
  const args = [
    c.tools.cmake,
    '-S',
    c.source_dir,
    '-B',
    build,
    '-G',
    c.build.generator,
    `-DCMAKE_BUILD_TYPE=${c.build.type}`,
    `-DCMAKE_C_COMPILER=${c.tools.cc}`,
    `-DCMAKE_CXX_COMPILER=${c.tools.cxx}`,
    '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
    '-DCMAKE_C_COMPILER_LAUNCHER=',
    '-DCMAKE_CXX_COMPILER_LAUNCHER=',
    `-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=${moduleFile}`,
    `-DPGO_PHASE=${phase}`,
    `-DPGO_PROFILE_DIR=${path.join(c.run_dir, 'profile-live')}`,
    `-DPGO_TARGETS=${c.build.targets.join(';')}`,
    `-DPGO_LINK_TARGETS=${c.build.link_targets.join(';')}`,
    `-DPGO_THREADED=${c.workload.process_model === 'joined_threads' ? 'ON' : 'OFF'}`,
    ...c.build.configure_args,
  ];
  run(c, state, args, { label: `configure-${phase}` });
  const built = run(
    c,
    state,
    [
      c.tools.cmake,
      '--build',
      build,
      '--parallel',
      String(c.resources.jobs),
      '--verbose',
      '--target',
      ...new Set([...c.build.targets, ...c.build.link_targets]),
    ],
    { label: `build-${phase}` },
  );
  if (
    /missing-profile|coverage-mismatch|profile.*(?:not found|out of date)/i.test(
      built.stdout + built.stderr,
    )
  )
    fail('profile_build_diagnostic', '构建含 profile 缺失或不一致诊断');
  const db = readJSON(path.join(build, 'compile_commands.json'));
  const audited = [];
  for (const target of c.build.targets) {
    const entries = db.filter((x) =>
      (x.command ?? x.arguments?.join(' ') ?? '').includes(`CMakeFiles/${target}.dir/`),
    );
    if (!entries.length)
      fail('target_not_compiled', `没有目标编译命令: ${target}（OBJECT/外部项目需单独适配）`);
    for (const entry of entries) {
      const cmd = entry.command ?? entry.arguments.join(' ');
      if (phase === 'B' && /-f(?:profile|auto-profile)/.test(cmd))
        fail('baseline_instrumented', target);
      if (phase === 'G' && (!cmd.includes('-fprofile-generate=') || cmd.includes('-fprofile-use')))
        fail('generation_flags_missing', target);
      if (phase === 'P' && (!cmd.includes('-fprofile-use=') || cmd.includes('-fprofile-generate')))
        fail('use_flags_missing', target);
      if (/-Wno-(?:error=)?(?:missing-profile|coverage-mismatch)|-fprofile-correction/.test(cmd))
        fail('diagnostics_suppressed', target);
      audited.push({ target, file: entry.file, command: cmd });
    }
  }
  const links = [];
  for (const target of c.build.link_targets) {
    const matches = files(build, 'link.txt').filter((p) =>
      p.replaceAll('\\', '/').endsWith(`/CMakeFiles/${target}.dir/link.txt`),
    );
    if (matches.length !== 1) fail('link_evidence_missing', target);
    const cmd = fs.readFileSync(matches[0], 'utf8');
    links.push({ target, command: cmd });
    if (phase === 'B' && /-f(?:profile|auto-profile)/.test(cmd))
      fail('baseline_instrumented', target);
    if (phase === 'G' && !cmd.includes('-fprofile-generate='))
      fail('generation_link_missing', target);
    if (phase === 'P' && !cmd.includes('-fprofile-use=')) fail('use_link_missing', target);
    for (const lib of c.libraries.filter((x) => x.kind === 'static' && x.link_target === target)) {
      const map = path.join(build, `pgo-${target}.map`);
      if (!fs.existsSync(map) || !mapContainsArchive(fs.readFileSync(map, 'utf8'), build, lib.path))
        fail('wrong_static_library', `链接映射不能证明包含 ${lib.path}`);
    }
  }
  const hashes = artifactHashes(c);
  for (const name of c.artifacts)
    if (!c.libraries.some((l) => l.kind === 'static' && l.path === name))
      checkElf64Arm(path.join(build, name));
  if (phase !== 'B') {
    const baseline = readJSON(path.join(c.run_dir, 'build-B.json')).commands;
    if (baseline.length !== audited.length) fail('build_not_equivalent', '编译单元数量变化');
    for (const entry of audited) {
      const old = baseline.find((x) => x.target === entry.target && x.file === entry.file);
      if (
        !old ||
        JSON.stringify(normalizeCommand(old.command)) !==
          JSON.stringify(normalizeCommand(entry.command))
      )
        fail('build_not_equivalent', `非 PGO 编译参数变化: ${entry.file}`);
    }
    const baseLinks = readJSON(path.join(c.run_dir, 'build-B.json')).links;
    for (const link of links) {
      const old = baseLinks.find((x) => x.target === link.target);
      if (
        !old ||
        JSON.stringify(normalizeCommand(old.command)) !==
          JSON.stringify(normalizeCommand(link.command))
      )
        fail('link_not_equivalent', `非 PGO 链接参数变化: ${link.target}`);
    }
  }
  writeJSON(path.join(c.run_dir, `build-${phase}.json`), {
    phase,
    hashes,
    commands: audited,
    links,
  });
  return hashes;
}
export function snapshot(c, phase) {
  const dest = path.join(c.run_dir, 'artifacts', phase);
  if (fs.existsSync(dest)) fail('snapshot_exists', dest);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.cpSync(path.join(c.run_dir, 'build'), dest, { recursive: true, verbatimSymlinks: true });
}
export function restore(c, phase) {
  const expected = readJSON(path.join(c.run_dir, `build-${phase}.json`)).hashes;
  removeOwned(c.run_dir, 'build');
  fs.cpSync(path.join(c.run_dir, 'artifacts', phase), path.join(c.run_dir, 'build'), {
    recursive: true,
    verbatimSymlinks: true,
  });
  if (JSON.stringify(artifactHashes(c)) !== JSON.stringify(expected))
    fail('artifact_changed', phase);
}
