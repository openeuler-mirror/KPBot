#!/usr/bin/env node
/** GCC PGO 分阶段执行器。stdout 为 JSON 契约，日志保存在实验目录。 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  assertContract,
  fail,
  readJSON,
  writeJSON,
  fileHash,
  sha,
  treeHash,
} from './lib/contract.mjs';
import {
  inspect,
  environmentGate,
  fingerprint,
  assertFingerprint,
  assertOwned,
  run,
  expand,
  files,
  removeOwned,
  artifactHashes,
} from './lib/runtime.mjs';
import { configure, snapshot, restore } from './lib/cmake.mjs';
import { compare } from './lib/statistics.mjs';
import { stages, validateState, receipt } from './lib/state.mjs';

function checkVersions(c, state) {
  const versions = {};
  for (const [name, tool] of Object.entries(c.tools))
    versions[name] = run(c, state, [tool, '--version'], { label: `version-${name}` }).stdout;
  if (/clang/i.test(versions.cc + versions.cxx)) fail('not_gcc', '首版不接受 Clang/LLVM');
  const triple = run(c, state, [c.tools.cc, '-dumpmachine'], {
    label: 'target-triple',
  }).stdout.trim();
  if (!/^aarch64.*linux/.test(triple)) fail('not_native_aarch64', triple);
  const cppTriple = run(c, state, [c.tools.cxx, '-dumpmachine'], {
    label: 'cxx-triple',
  }).stdout.trim();
  if (cppTriple !== triple) fail('toolchain_mismatch', 'CC/CXX target 不同');
  const gccVersion = run(c, state, [c.tools.cc, '-dumpfullversion'], {
    label: 'gcc-version',
  }).stdout.trim();
  const cppVersion = run(c, state, [c.tools.cxx, '-dumpfullversion'], {
    label: 'gxx-version',
  }).stdout.trim();
  if (
    gccVersion !== cppVersion ||
    !versions.gcov_tool.includes(gccVersion) ||
    !versions.gcov_dump.includes(gccVersion)
  )
    fail('toolchain_mismatch', '要求 CC/CXX/gcov-tool/gcov-dump 精确 GCC 版本一致');
  const cm = versions.cmake.match(/cmake version (\d+)\.(\d+)/);
  if (!cm || Number(cm[1]) < 3 || (Number(cm[1]) === 3 && Number(cm[2]) < 24))
    fail('cmake_version', '要求 CMake >= 3.24');
  writeJSON(path.join(c.run_dir, 'environment.json'), {
    ...inspect(),
    versions,
    triple,
    gccVersion,
  });
}
function checkShared(c, stderr) {
  for (const lib of c.libraries.filter((x) => x.kind === 'shared')) {
    const absolute = fs.realpathSync(path.join(c.run_dir, 'build', lib.path));
    // glibc LD_DEBUG 的实际初始化记录；仅搜索文件名会误认搜索候选。
    const lines = stderr.split('\n').filter((x) => x.includes('calling init:'));
    if (
      !lines.some((line) => {
        const candidate = line.split('calling init:')[1].trim();
        try {
          return fs.realpathSync(candidate) === absolute;
        } catch {
          return false;
        }
      })
    )
      fail('shared_identity_unproven', `加载器记录未证明加载目标库 ${absolute}`);
  }
}
function workload(c, state, argv, label, { identity = true, metric = false } = {}) {
  const result = run(c, state, expand(c, argv), {
    cwd: path.join(c.run_dir, 'build'),
    env: identity ? { LD_DEBUG: 'files' } : {},
    label,
  });
  if (identity) checkShared(c, result.stderr);
  let data;
  try {
    data = JSON.parse(result.stdout.trim());
  } catch {
    fail('driver_protocol', `${label} stdout 必须只输出一个 JSON 对象`);
  }
  if (data.correct !== true) fail('correctness_failed', `${label} 未返回 correct:true`);
  if (
    metric &&
    (!Number.isFinite(data.metrics?.[c.measurement.metric]) ||
      data.metrics[c.measurement.metric] <= 0)
  )
    fail('invalid_metric', label);
  return data;
}
function checkProfiles(c, state) {
  const profile = path.join(c.run_dir, 'profile-live'),
    found = files(profile, '.gcda');
  if (!found.length) fail('empty_profile', '没有生成 GCC gcda');
  const list = [];
  for (const p of found) {
    if (fs.statSync(p).size < 16) fail('empty_profile', p);
    const d = run(c, state, [c.tools.gcov_dump, '-l', p], { label: 'gcov-dump' });
    if (/(?:not a gcov|version.*(?:mismatch|expected)|error|invalid)/i.test(d.stdout + d.stderr))
      fail('invalid_profile', p);
    // gcov-dump 的 OBJECT_SUMMARY 可验证执行过，不要求人为指定全局覆盖率。
    if (!/OBJECT_SUMMARY.*runs=[1-9]/.test(d.stdout))
      fail('profile_not_executed', `gcov-dump 无有效 runs: ${p}`);
    list.push({ file: path.relative(profile, p), sha256: fileHash(p) });
  }
  writeJSON(path.join(c.run_dir, 'profile-manifest.json'), list);
}
function profileDigest(c) {
  return sha(
    JSON.stringify(
      files(path.join(c.run_dir, 'profile-live'), '.gcda').map((p) => [
        path.relative(c.run_dir, p),
        fileHash(p),
      ]),
    ),
  );
}
function assertProfile(c, state) {
  if (profileDigest(c) !== state.profile_digest)
    fail('profile_changed', '采集产物被修改或缺失，拒绝重编译/恢复');
}
function report(c, state) {
  const r = state.result;
  const result = {
    success: state.completed.includes('compare'),
    execution_status: state.completed.includes('compare') ? 'completed' : 'ready',
    optimization_outcome: r?.optimization_outcome ?? 'not_evaluated',
    completed_stages: state.completed,
    next_stage: stages[state.completed.length] ?? null,
    result: r ?? null,
    run_dir: c.run_dir,
    limitations: [
      '只对批准的训练/验证分布成立',
      '未自动安装或替换生产库',
      'driver 的正确性自检需要用户确认其充分性',
    ],
  };
  writeJSON(path.join(c.run_dir, 'report.json'), result);
  fs.writeFileSync(
    path.join(c.run_dir, 'report.md'),
    `# GCC PGO 实验报告\n\n- 执行状态：${result.execution_status}\n- 优化结论：${result.optimization_outcome}\n- 已完成阶段：${state.completed.join(', ')}\n- 输入与来源：experiment.json\n- 环境：environment.json\n- 命令与完整输出：logs/\n- 原始测量：measurements.json\n- 详细结果：report.json\n\n${r ? `加权收益：${r.gain_pct.toFixed(3)}%；95% 区间：[${r.interval_pct.map((x) => x.toFixed(3)).join(', ')}]%。\n\n` : ''}不承诺未测试业务收益；无收益或退化也是有效实验结果。候选产物位于 artifacts/P；恢复基线使用 artifacts/B，保持同一加载路径。\n`,
  );
  return { pgo_result: result };
}
export function execute(action, c) {
  assertContract(c);
  if (action === 'validate-input')
    return {
      pgo_result: {
        success: true,
        execution_status: 'ready',
        optimization_outcome: 'not_evaluated',
      },
    };
  if (action === 'plan')
    return {
      pgo_result: {
        success: true,
        execution_status: 'ready',
        stages,
        source_dir: c.source_dir,
        run_dir: c.run_dir,
        training: c.training,
        validation: c.validation,
        measurement: c.measurement,
        note: '计划不执行构建和负载；通过实际环境门控后才能运行。',
      },
    };
  environmentGate(c);
  const root = c.run_dir,
    stateFile = path.join(root, 'state.json');
  if (action === 'init') {
    const fp = fingerprint(c);
    if (fs.existsSync(root)) fail('run_dir_exists', '实验目录必须不存在；不得覆盖旧实验');
    let ancestor = path.dirname(root);
    while (!fs.existsSync(ancestor)) ancestor = path.dirname(ancestor);
    if (fs.realpathSync(ancestor) !== path.resolve(ancestor))
      fail('run_dir_symlink', '实验目录的祖先含符号链接');
    fs.mkdirSync(root, { recursive: true });
    if (fs.realpathSync(root) !== path.resolve(root))
      fail('run_dir_symlink', '实验路径不能经由符号链接');
    writeJSON(path.join(root, '.pgo-owned.json'), { root: path.resolve(root) });
    writeJSON(path.join(root, 'experiment.json'), c);
    const state = {
      version: 1,
      started_ms: Date.now(),
      command_index: 0,
      completed: [],
      fingerprint: fp,
    };
    writeJSON(stateFile, state);
    try {
      checkVersions(c, state);
      state.initialized = true;
      writeJSON(stateFile, state);
    } catch (e) {
      writeJSON(stateFile, state);
      throw e;
    }
    return { pgo_result: { success: true, execution_status: 'ready', next_stage: 'baseline' } };
  }
  assertOwned(root);
  const lock = path.join(root, '.lock');
  let fd;
  try {
    fd = fs.openSync(lock, 'wx');
  } catch {
    fail('experiment_locked', '实验正被另一进程使用；意外退出时先核实无进程再移除 .lock');
  }
  let state;
  try {
    state = readJSON(stateFile);
    if (!state.initialized)
      fail('initialization_failed', '工具链初始化未完成，请修正条件后创建新实验');
    assertFingerprint(c, state.fingerprint);
    validateState(root, state);
    if (state.profile_digest) assertProfile(c, state);
    for (const phase of ['B', 'G', 'P'])
      if (
        state.artifact_digests?.[phase] &&
        treeHash(path.join(root, 'artifacts', phase), true) !== state.artifact_digests[phase]
      )
        fail('snapshot_changed', phase);
    if (action === 'status')
      return {
        pgo_result: {
          success: true,
          completed_stages: state.completed,
          next_stage: stages[state.completed.length] ?? null,
          last_error: state.last_error ?? null,
        },
      };
    if (action === 'report') return report(c, state);
    const next = stages[state.completed.length];
    if (action === 'resume') action = next;
    if (!action) return report(c, state);
    if (action !== next) fail('stage_order', `下一阶段必须为 ${next}，不能执行 ${action}`);
    if (state.interrupted)
      fail(
        'interrupted_stage',
        `阶段 ${state.interrupted} 未完成；保留日志并新建实验，禁止复用部分产物`,
      );
    state.interrupted = action;
    state.stage_command_start = state.command_index;
    writeJSON(stateFile, state);
    switch (action) {
      case 'baseline':
        configure(c, state, 'B');
        workload(c, state, c.correctness.argv, 'correctness-B');
        snapshot(c, 'B');
        break;
      case 'instrument':
        removeOwned(root, 'profile-live');
        configure(c, state, 'G');
        snapshot(c, 'G');
        break;
      case 'train': {
        restore(c, 'G');
        const dirs = [];
        for (const s of c.training) {
          removeOwned(root, 'profile-live');
          for (let i = 0; i < s.runs; i++) workload(c, state, s.argv, `train-${s.id}-${i}`);
          checkProfiles(c, state);
          const dest = path.join(root, 'profiles', s.id);
          fs.mkdirSync(path.dirname(dest), { recursive: true });
          fs.cpSync(path.join(root, 'profile-live'), dest, { recursive: true });
          dirs.push(dest);
        }
        removeOwned(root, 'profile-live');
        fs.cpSync(dirs[0], path.join(root, 'profile-live'), { recursive: true });
        for (let i = 1; i < dirs.length; i++) {
          const output = path.join(root, 'profiles', `merged-${i}`);
          run(
            c,
            state,
            [c.tools.gcov_tool, 'merge', path.join(root, 'profile-live'), dirs[i], '-o', output],
            { label: 'profile-merge' },
          );
          removeOwned(root, 'profile-live');
          fs.cpSync(output, path.join(root, 'profile-live'), { recursive: true });
        }
        state.profile_digest = profileDigest(c);
        break;
      }
      case 'check-profile':
        checkProfiles(c, state);
        break;
      case 'optimize':
        configure(c, state, 'P');
        snapshot(c, 'P');
        break;
      case 'verify':
        for (const phase of ['B', 'P']) {
          restore(c, phase);
          workload(c, state, c.correctness.argv, `correctness-${phase}`);
          for (const s of c.validation) workload(c, state, s.argv, `validate-${phase}-${s.id}`);
        }
        break;
      case 'compare': {
        const samples = Object.fromEntries(c.validation.map((s) => [s.id, []]));
        for (let i = 0; i < c.measurement.pairs; i++) {
          const pair = Object.fromEntries(c.validation.map((s) => [s.id, {}]));
          for (const phase of i % 2 ? ['P', 'B'] : ['B', 'P']) {
            restore(c, phase);
            // 身份检测与 LD_DEBUG 不进入计时负载，避免日志影响测量。
            workload(c, state, c.correctness.argv, `identity-${phase}-${i}`);
            for (const s of c.validation) {
              for (let w = 0; w < c.measurement.warmups; w++)
                workload(c, state, s.argv, `warm-${phase}-${i}-${s.id}-${w}`, { identity: false });
              const d = workload(c, state, s.argv, `measure-${phase}-${i}-${s.id}`, {
                identity: false,
                metric: true,
              });
              pair[s.id][phase] = d.metrics[c.measurement.metric];
            }
          }
          for (const s of c.validation) samples[s.id].push(pair[s.id]);
          writeJSON(path.join(root, 'measurements.json'), samples);
        }
        state.result = compare(c, samples);
        restore(c, 'P');
        break;
      }
      default:
        fail('unknown_action', String(action));
    }
    assertFingerprint(c, state.fingerprint);
    state.artifact_digests ??= {};
    for (const phase of ['B', 'G', 'P'])
      if (fs.existsSync(path.join(root, 'artifacts', phase)) && !state.artifact_digests[phase])
        state.artifact_digests[phase] = treeHash(path.join(root, 'artifacts', phase), true);
    receipt(root, state, action);
    delete state.interrupted;
    delete state.last_error;
    state.completed.push(action);
    writeJSON(stateFile, state);
    return report(c, state);
  } catch (e) {
    if (state) {
      state.last_error = { code: e.code ?? 'unexpected_error', message: e.message };
      writeJSON(stateFile, state);
    }
    throw e;
  } finally {
    fs.closeSync(fd);
    fs.unlinkSync(lock);
  }
}
export function main(args) {
  try {
    const [action, config, ...extra] = args;
    if (action === 'inspect' && !config)
      return { pgo_result: { success: true, environment: inspect() } };
    if (action === '--help' || !action)
      return {
        usage:
          'node scripts/pgo.mjs inspect | <validate-input|plan|init|baseline|instrument|train|check-profile|optimize|verify|compare|resume|status|report> experiment.json',
      };
    if (!config || extra.length) fail('arguments', '需要且只接受一个 experiment.json 路径');
    if (
      !['validate-input', 'plan', 'init', ...stages, 'resume', 'status', 'report'].includes(action)
    )
      fail('unknown_action', action);
    return execute(action, readJSON(config));
  } catch (e) {
    process.exitCode = 2;
    return {
      pgo_result: {
        success: false,
        execution_status: e.status ?? 'failed',
        optimization_outcome: 'not_evaluated',
        code: e.code ?? 'unexpected_error',
        error_message: e.message,
        blockers: e.blockers ?? [],
        next_action: '解决明确问题后重试；不得补造数据或修改验收阈值',
      },
    };
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  console.log(JSON.stringify(main(process.argv.slice(2)), null, 2));
