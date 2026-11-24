import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {METRICS, L1, args, main, writeJson} from './common.mjs';

// Strict full-field parsing: signs, grouping and unavailable values are retained.
export function parseValue(raw, metric = null) {
  if (raw.includes('%') && !L1.includes(metric)) {
    return {value: null, status: 'unrecognized'};
  }
  const cleaned = raw.trim().replace(/%$/, '').trim();
  if (/^(?:N\/A|<not counted>|<not supported>|not counted|not supported)$/i.test(cleaned)) {
    return {value: null, status: 'unavailable'};
  }
  if (/^[+-]?(?:nan|inf|infinity)$/i.test(cleaned)) {
    return {value: null, status: 'nonfinite'};
  }
  const decimal = /^[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
  if (!decimal.test(cleaned)) return {value: null, status: 'unrecognized'};
  const value = Number(cleaned.replaceAll(',', ''));
  if (!Number.isFinite(value)) return {value: null, status: 'nonfinite'};
  return {value, status: 'parsed'};
}

export function inspect(input, source = 'memory') {
  // File callers pass the original bytes; strings are UTF-8 in-memory fixtures.
  const bytes = Buffer.isBuffer(input) ? input : Buffer.from(input, 'utf8');
  const text = bytes.toString('utf8');
  const rows = [];
  const warnings = [];
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  for (const [index, original] of lines.entries()) {
    const line = original.replace(/\x1b\[[0-9;]*m/g, '');
    for (const name of METRICS) {
      const match = line.match(new RegExp(`^\\s*${name}\\s*(?::|\\s)\\s*(.*?)\\s*$`, 'i'));
      if (!match) continue;
      const raw = match[1];
      const numeric = parseValue(raw, name);
      if (['Cycles', 'Instructions'].includes(name) && numeric.status === 'parsed' &&
          !Number.isSafeInteger(numeric.value)) {
        numeric.value = null;
        numeric.status = 'unsafe_count';
      }
      rows.push({
        id: `line-${index + 1}`,
        name,
        raw,
        ...numeric,
        line: index + 1,
        source,
        unit: name === 'IPC' ? 'instructions/cycle' :
          METRICS.indexOf(name) >= 3 ? '%' : 'count',
      });
      break;
    }
    if (/warning|error|ignored|ignore\b|failed|not supported|not counted|segmentation|SIGSEGV|警告|忽略|失败/i.test(line)) {
      warnings.push({line: index + 1, text: original});
    }
  }
  return {
    schema_version: 1,
    source,
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    adapter: 'devkit-l1-english-lines-v1',
    rows,
    warnings,
    limitations: [
      'No automatic summary/window selection.',
      'Only seven named summary fields are parsed; other events remain in the raw log.',
      'Text recognition does not certify platform or metric semantics.',
    ],
  };
}

main(import.meta.url, () => {
  const opt = args(process.argv.slice(2), ['log', 'out']);
  const bytes = fs.readFileSync(opt.log);
  writeJson(opt.out, inspect(bytes, path.resolve(opt.log)));
});
