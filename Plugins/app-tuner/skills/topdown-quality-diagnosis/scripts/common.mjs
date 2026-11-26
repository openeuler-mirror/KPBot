import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const METRICS = [
  'Cycles',
  'Instructions',
  'IPC',
  'Bad Speculation',
  'Frontend Bound',
  'Retiring',
  'Backend Bound',
];

export const L1 = METRICS.slice(3);

export function readJson(filename) {
  const text = fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, '');
  return JSON.parse(text);
}

export function writeNew(filename, text) {
  fs.mkdirSync(path.dirname(path.resolve(filename)), {recursive: true});
  fs.writeFileSync(filename, text, {encoding: 'utf8', flag: 'wx'});
}

export function writeJson(filename, value) {
  writeNew(filename, JSON.stringify(value, null, 2) + '\n');
}

export function args(argv, required, optional = []) {
  const result = {};
  const allowed = new Set([...required, ...optional]);
  for (let i = 0; i < argv.length; i += 2) {
    const name = argv[i]?.replace(/^--/, '');
    if (!argv[i]?.startsWith('--') || !allowed.has(name)) {
      throw new Error(`Unknown option: ${argv[i]}`);
    }
    if (!argv[i + 1] || argv[i + 1].startsWith('--') || name in result) {
      throw new Error(`Missing or duplicate value: ${name}`);
    }
    result[name] = argv[i + 1];
  }
  for (const name of required) {
    if (!(name in result)) throw new Error(`Required: --${name}`);
  }
  return result;
}

export function main(url, action) {
  if (!process.argv[1] || path.resolve(process.argv[1]) !== fileURLToPath(url)) return;
  try {
    action();
  } catch (error) {
    console.error(`ERROR: ${error.message}`);
    process.exitCode = 2;
  }
}

export function finite(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

export function evidence(item) {
  return item && typeof item.evidence === 'string' && item.evidence.trim().length > 0;
}

export function aggregate(checks) {
  for (const state of ['invalid', 'unknown', 'limited']) {
    if (checks.some(check => check.status === state)) return state;
  }
  return 'pass';
}

export function escapeCell(value) {
  return String(value ?? 'unknown')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('|', '&#124;')
    .replaceAll('`', '&#96;')
    .replaceAll('[', '&#91;')
    .replaceAll(']', '&#93;')
    .replace(/[\r\n]+/g, ' ');
}
