import fs from 'node:fs';
import path from 'node:path';
import { fail, readJSON, fileHash, sha, writeJSON } from './contract.mjs';
export const stages = [
  'baseline',
  'instrument',
  'train',
  'check-profile',
  'optimize',
  'verify',
  'compare',
];
export function validateState(root, state) {
  if (
    !Array.isArray(state.completed) ||
    state.completed.some((s, i) => s !== stages[i]) ||
    state.completed.length > stages.length
  )
    fail('invalid_state', '阶段列表不合法');
  let previous = null;
  for (const stage of state.completed) {
    const receiptFile = path.join(root, 'receipts', `${stage}.json`);
    if (!fs.existsSync(receiptFile)) fail('missing_receipt', `缺少阶段证据: ${stage}`);
    const receipt = readJSON(receiptFile);
    if (
      receipt.stage !== stage ||
      receipt.previous !== previous ||
      receipt.contract !== state.fingerprint.contract
    )
      fail('invalid_receipt', stage);
    for (const [name, hash] of Object.entries(receipt.files)) {
      const p = path.resolve(root, name);
      if (!p.startsWith(path.resolve(root) + path.sep) || !fs.existsSync(p) || fileHash(p) !== hash)
        fail('stage_evidence_changed', `${stage}: ${name}`);
    }
    previous = fileHash(receiptFile);
  }
  for (const [stage, phase] of [
    ['baseline', 'B'],
    ['instrument', 'G'],
    ['optimize', 'P'],
  ])
    if (state.completed.includes(stage) && !state.artifact_digests?.[phase])
      fail('missing_snapshot_digest', phase);
  if (state.completed.includes('train') && !state.profile_digest)
    fail('missing_profile_digest', '缺少训练证据');
  if (state.completed.includes('compare') && !state.result)
    fail('missing_comparison', '缺少对比结果');
}
export function receipt(root, state, stage) {
  const files = {};
  const add = (name) => {
    const p = path.join(root, name);
    if (!fs.existsSync(p)) fail('missing_stage_evidence', name);
    files[name] = fileHash(p);
  };
  const phase = { baseline: 'B', instrument: 'G', optimize: 'P' }[stage];
  if (phase) add(`build-${phase}.json`);
  if (stage === 'check-profile') add('profile-manifest.json');
  if (stage === 'compare') add('measurements.json');
  // 所有阶段都有执行日志；将当前阶段命令及其输出纳入证据链。
  const start = state.stage_command_start;
  for (const name of fs.readdirSync(path.join(root, 'logs'))) {
    const index = Number(name.slice(0, 5));
    if (index > start && index <= state.command_index) add(`logs/${name}`);
  }
  const last = state.completed.at(-1);
  writeJSON(path.join(root, 'receipts', `${stage}.json`), {
    stage,
    contract: state.fingerprint.contract,
    previous: last ? fileHash(path.join(root, 'receipts', `${last}.json`)) : null,
    files,
  });
}
