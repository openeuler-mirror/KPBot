#!/usr/bin/env node
// Dependency-free evidence collector/parser. Never interprets a metric as a cause.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const ROOT_METRICS = [
  'Bad Speculation', 'Frontend Bound', 'Retiring', 'Backend Bound',
];
const NUMBER = /^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$/;
const MISSING = /^(?:N\/A|nan|[-—]|<not supported>|<not counted>)$/i;
const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const MAX_BYTES = 32 * 1024 * 1024;

export function digest(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

export function numeric(token) {
  if (MISSING.test(token)) return null;
  if (!NUMBER.test(token)) return undefined;
  const value = Number(token.replaceAll(',', ''));
  return Number.isFinite(value) ? value : undefined;
}

function issue(list, code, line, detail) {
  list.push({ code, line, detail });
}

function newBlock(line, title) {
  return {
    block_id: null,
    start_line: line,
    title,
    context_lines: [],
    counters: {},
    metrics: [],
    pmu_events: [],
    warnings: [],
  };
}

function validateBlock(block) {
  const roots = block.metrics.filter(row => ROOT_METRICS.includes(row.name));
  for (const name of ROOT_METRICS) {
    const rows = roots.filter(row => row.name === name);
    if (rows.length !== 1) {
      issue(block.warnings, 'root_count', block.start_line,
        `${name}: expected one row, found ${rows.length}`);
    }
  }
  if (roots.length === 4 && ROOT_METRICS.every(name =>
    roots.filter(row => row.name === name).length === 1) &&
    roots.every(row => row.value !== null)) {
    const total = roots.reduce((sum, row) => sum + row.value, 0);
    block.level1_sum = Number(total.toFixed(4));
    if (Math.abs(total - 100) > 1) {
      issue(block.warnings, 'level1_sum', block.start_line,
        `Displayed Level1 sum ${total}; verify definitions and completeness`);
    }
  }
  for (const row of block.metrics) {
    if (row.value === null || row.value < 0 || row.value > 100) {
      issue(block.warnings, 'metric_value', row.line,
        `${row.name}: missing or outside displayed percentage range`);
    }
  }
  for (const name of ['Cycles', 'Instructions']) {
    const counter = block.counters[name];
    if (!counter || counter.value === null || counter.value <= 0) {
      issue(block.warnings, 'counter_missing_or_zero', block.start_line, name);
    }
  }
}

export function parseText(text, source = {}) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  const warnings = [];
  let block;
  let section = 'none';
  let tableSeen = false;
  function begin(line, title) {
    block = newBlock(line, title);
    blocks.push(block);
    section = 'context';
    tableSeen = false;
  }
  for (let index = 0; index < lines.length; index++) {
    const raw = lines[index];
    const clean = raw.replace(ANSI, '');
    const trimmed = clean.trim();
    const line = index + 1;
    if (/error:|PmuOpen failed|LIBPERF|not supported|not counted/i.test(clean)) {
      issue(warnings, 'tool_diagnostic', line, trimmed);
    }
    if (/^Top-down metrics of\b/i.test(trimmed)) {
      begin(line, trimmed);
      continue;
    }
    if (/^Top-down Metrics\s+Bound\(%\)\s*$/i.test(trimmed)) {
      if (!block || tableSeen) begin(line, 'Table without scope header');
      section = 'metrics';
      tableSeen = true;
      continue;
    }
    if (!block) continue;
    if (/^PMU Event\s+Count\s*$/i.test(trimmed)) {
      section = 'pmu';
      continue;
    }
    if (/^Note:/.test(trimmed)) {
      section = 'context';
    }
    const counter = trimmed.match(/^(Cycles|Instructions|IPC)\s+(.+)$/);
    if (counter) {
      const value = numeric(counter[2]);
      if (Object.hasOwn(block.counters, counter[1])) {
        issue(block.warnings, 'duplicate_counter', line, counter[1]);
      }
      block.counters[counter[1]] = {
        value: value ?? null, raw: counter[2], line,
      };
      if (value === undefined) issue(block.warnings, 'unknown_counter', line, trimmed);
      continue;
    }
    if (!trimmed || /^[-=]+$/.test(trimmed)) continue;
    if (section === 'metrics') {
      const match = clean.match(/^(.*\S)\s{2,}(\S(?:.*\S)?)\s*$/);
      if (!match) {
        issue(block.warnings, 'unparsed_metric_line', line, trimmed);
        continue;
      }
      const value = numeric(match[2].replace(/%$/, ''));
      const prefix = match[1].match(/^[\s│├└─+|`\\]*/)?.[0] ?? '';
      const name = match[1].slice(prefix.length).trim();
      if (!name || value === undefined) {
        issue(block.warnings, 'unparsed_metric_line', line, trimmed);
        continue;
      }
      block.metrics.push({
        name, value, unit: 'percent',
        denominator: 'tool-defined/unverified',
        tree_prefix: prefix,
        line, raw,
      });
    } else if (section === 'pmu') {
      const match = trimmed.match(/^(r[0-9a-f]+)\s+(.+)$/i);
      if (!match) {
        issue(block.warnings, 'unparsed_pmu_line', line, trimmed);
        continue;
      }
      // Keep integer counts as strings; JS Number can lose precision above 2^53.
      const valid = /^(?:\d{1,3}(?:,\d{3})+|\d+)$/.test(match[2]);
      block.pmu_events.push({
        event: match[1], count: valid ? match[2].replaceAll(',', '') : null,
        raw: match[2], line,
      });
      if (!valid) issue(block.warnings, 'invalid_pmu_count', line, trimmed);
    } else {
      block.context_lines.push({ line, text: raw });
    }
  }
  blocks.forEach((entry, index) => {
    entry.block_id = `block-${index + 1}`;
    validateBlock(entry);
  });
  if (!blocks.length) issue(warnings, 'no_supported_table', null,
    'No supported DevKit table found; retain original and inspect manually');
  const count = blocks.reduce((sum, entry) => sum + entry.metrics.length, 0);
  const hasWarnings = warnings.length || blocks.some(entry => entry.warnings.length);
  return {
    schema_version: '1.0',
    source_skill: 'topdown-analysis-advisor',
    parser: 'devkit-text-v1',
    source,
    status: !count ? 'unusable' : hasWarnings ? 'needs_review' : 'ready_for_review',
    warnings,
    blocks,
    interpretation: 'Human review required; no automatic root-cause inference',
  };
}

export function readLimited(filename) {
  const stat = fs.statSync(filename);
  if (!stat.isFile() || stat.size > MAX_BYTES) {
    throw new Error('Input must be a regular file no larger than 32 MiB');
  }
  const bytes = fs.readFileSync(filename);
  if (bytes.length > MAX_BYTES) throw new Error('Input grew beyond 32 MiB');
  return bytes;
}

export function writeNewJson(filename, data) {
  fs.writeFileSync(filename, `${JSON.stringify(data, null, 2)}\n`, {
    encoding: 'utf8', flag: 'wx',
  });
}

function positive(text, label, max) {
  if (!/^\d+$/.test(text ?? '') || Number(text) < 1 || Number(text) > max) {
    throw new Error(`${label} must be an integer from 1 to ${max}`);
  }
  return Number(text);
}

export function collectionArgs(options) {
  const duration = positive(options.duration, 'duration', 3600);
  const interval = positive(options.interval ?? '1', 'interval', duration);
  const level = options.level ?? '1';
  if (!/^[0-6]$/.test(level)) throw new Error('level must be 0..6');
  const scope = options.scope ?? 'all';
  if (!['all', 'user', 'kernel'].includes(scope)) throw new Error('Invalid scope');
  if (Boolean(options.pid) === Boolean(options.cpus)) {
    throw new Error('Specify exactly one of --pid or --cpus');
  }
  if (options.pid && !/^[1-9]\d*(?:,[1-9]\d*)*$/.test(options.pid)) {
    throw new Error('PID must be a comma-separated list of positive integers');
  }
  if (options.cpus) {
    if (!/^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(options.cpus)) {
      throw new Error('Invalid CPU list');
    }
    for (const part of options.cpus.split(',')) {
      const [start, end = start] = part.split('-').map(Number);
      if (!Number.isSafeInteger(end) || end < start) throw new Error('Invalid CPU range');
    }
  }
  return ['tuner', 'top-down', '-d', String(duration), '-i', String(interval),
    '-L', level, '-r', scope, options.pid ? '-p' : '-c', options.pid ?? options.cpus];
}

// Executable and argv are separate; no shell interpolation of user input.
export async function capture(executable, argv, directory, timeoutMs) {
  const stdoutPath = path.join(directory, 'stdout.txt');
  const stderrPath = path.join(directory, 'stderr.txt');
  const stdout = fs.openSync(stdoutPath, 'wx');
  let stderr;
  try {
    stderr = fs.openSync(stderrPath, 'wx');
  } catch (error) {
    fs.closeSync(stdout);
    throw error;
  }
  const started = new Date().toISOString();
  let child;
  try {
    child = spawn(executable, argv, {
      shell: false,
      detached: process.platform !== 'win32',
      stdio: ['ignore', stdout, stderr],
      windowsHide: true,
    });
  } finally {
    fs.closeSync(stdout);
    fs.closeSync(stderr);
  }
  let timedOut = false;
  let spawnError = null;
  function stop(signal) {
    if (!child.pid) return;
    try {
      if (process.platform === 'win32') child.kill(signal);
      else process.kill(-child.pid, signal);
    } catch (error) {
      if (error.code !== 'ESRCH') spawnError ??= error.message;
    }
  }
  let forceTimer;
  const timer = setTimeout(() => {
    timedOut = true;
    stop('SIGTERM');
    forceTimer = setTimeout(() => stop('SIGKILL'), 1000);
  }, timeoutMs);
  const result = await new Promise(resolve => {
    child.once('error', error => { spawnError = error.message; });
    child.once('close', (code, signal) => resolve({ code, signal }));
  });
  clearTimeout(timer);
  // On Linux a child may exit before its descendants: finish the timed-out group.
  if (timedOut) stop('SIGKILL');
  clearTimeout(forceTimer);
  return {
    started_at: started,
    ended_at: new Date().toISOString(),
    exit_code: result.code,
    signal: result.signal,
    timed_out: timedOut,
    error: spawnError,
    status: timedOut ? 'timeout' : spawnError || result.code !== 0 ? 'failed' : 'collected',
  };
}

export async function collect(options) {
  if (process.platform !== 'linux' || process.arch !== 'arm64') {
    throw new Error('Online collection requires Linux aarch64; offline analyze is portable');
  }
  const argv = collectionArgs(options);
  if (!options.wrapper || !path.isAbsolute(options.wrapper)) {
    throw new Error('--wrapper must be an absolute run_devkit.sh path');
  }
  if (!fs.statSync(options.wrapper).isFile()) throw new Error('Wrapper is not a file');
  if (!options.out) throw new Error('--out is required');
  const directory = path.resolve(options.out);
  fs.mkdirSync(directory); // Existing directory fails: never overwrite evidence.
  const manifest = {
    schema_version: '1.0',
    source_skill: 'topdown-analysis-advisor',
    command: ['bash', options.wrapper, ...argv],
    target: options.pid ? { pid: options.pid } : { cpus: options.cpus },
    scope: options.scope ?? 'all',
    duration_seconds: Number(options.duration),
    profile: Number(options.level ?? '1'),
    environment: { platform: process.platform, architecture: process.arch },
    context: options.context ?? null,
  };
  const result = await capture('bash', [options.wrapper, ...argv], directory,
    (Number(options.duration) + 30) * 1000);
  Object.assign(manifest, result);
  manifest.files = ['stdout.txt', 'stderr.txt'].map(file => ({
    file, sha256: digest(fs.readFileSync(path.join(directory, file))),
  }));
  writeNewJson(path.join(directory, 'manifest.json'), manifest);
  return manifest;
}

export function analyze(options) {
  if (!options.input || !options.output) throw new Error('--input and --output are required');
  const bytes = readLimited(options.input);
  const summary = parseText(bytes.toString('utf8'), {
    file: path.resolve(options.input), sha256: digest(bytes), bytes: bytes.length,
  });
  if (options.manifest) {
    const manifest = JSON.parse(readLimited(options.manifest).toString('utf8'));
    const original = manifest.files?.find(file => file.file === 'stdout.txt');
    summary.collection = manifest;
    if (!original || original.sha256 !== summary.source.sha256) {
      issue(summary.warnings, 'manifest_mismatch', null, 'Input does not match manifest stdout');
    }
    if (manifest.status !== 'collected' || manifest.exit_code !== 0) {
      issue(summary.warnings, 'collection_failed', null, 'Collection did not complete successfully');
    }
    if (summary.warnings.length && summary.status !== 'unusable') summary.status = 'needs_review';
  }
  writeNewJson(options.output, summary);
  return summary;
}

export function parseOptions(args, allowed) {
  const options = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index]?.replace(/^--/, '');
    const value = args[index + 1];
    if (!args[index]?.startsWith('--') || !allowed.includes(key) ||
        value === undefined || value.startsWith('--') || Object.hasOwn(options, key)) {
      throw new Error(`Unknown, duplicate or incomplete option: ${args[index]}`);
    }
    options[key] = value;
  }
  return options;
}

async function main(args) {
  const [command, ...rest] = args;
  if (command === '--help' || !command) {
    console.log('topdown.mjs collect --wrapper ABS --out NEW_DIR --duration SEC (--pid IDS | --cpus LIST)');
    console.log('  optional: --interval SEC --level 0..6 --scope all|user|kernel --context TEXT');
    console.log('topdown.mjs analyze --input TEXT --output NEW_JSON [--manifest JSON]');
    return;
  }
  if (command === 'collect') {
    const result = await collect(parseOptions(rest,
      ['wrapper', 'out', 'duration', 'pid', 'cpus', 'interval', 'level', 'scope', 'context']));
    console.log(result.status);
    if (result.status !== 'collected') process.exitCode = 2;
  } else if (command === 'analyze') {
    const result = analyze(parseOptions(rest, ['input', 'output', 'manifest']));
    console.log(result.status);
    if (result.status !== 'ready_for_review') process.exitCode = 2;
  } else {
    throw new Error(`Unknown command: ${command}`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => {
    console.error(`ERROR: ${error.message}`);
    process.exitCode = 1;
  });
}
