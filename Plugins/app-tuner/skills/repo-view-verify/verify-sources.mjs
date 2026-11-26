#!/usr/bin/env node
// verify-sources.mjs — 零依赖源码证据验证器（repo-view-verify 自带资产）
// 用法: node verify-sources.mjs <candidate.ir.json> --repo-root <repo-root>
// 退出码: 0 = 全部通过；1 = 有失败项；2 = 用法/IR 结构错误
//
// 做三件事：
//   1. meta.repository.revision 必须是 40 位 SHA 且是本地真实 commit 对象
//   2. 递归收集 IR 中全部 {path, line, end_line?} 证据引用
//   3. 逐条用本地 git 验证：路径在该 commit 存在（cat-file -e），行号不越界（git show 数行）

import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';

const argv = process.argv.slice(2);
const irPath = argv[0];
const repoRootIdx = argv.indexOf('--repo-root');

function usage() {
  console.error('用法: node verify-sources.mjs <candidate.ir.json> --repo-root <repo-root>');
  process.exit(2);
}

if (!irPath || repoRootIdx === -1 || !argv[repoRootIdx + 1]) usage();
const repoRoot = argv[repoRootIdx + 1];

let ir;
try {
  ir = JSON.parse(readFileSync(irPath, 'utf8'));
} catch (e) {
  console.error(`无法读取/解析 IR: ${irPath}: ${e.message}`);
  process.exit(2);
}

const repo = ir.meta && ir.meta.repository;
const failures = [];

if (!repo || !repo.revision) {
  console.error('IR 结构错误: 缺少 meta.repository.revision（40 位 commit SHA）');
  process.exit(2);
}
const rev = String(repo.revision);
if (!/^[0-9a-fA-F]{40}$/.test(rev)) failures.push({ subject: '(repository)', problem: `revision 不是 40 位 SHA: ${rev}` });

function git(...a) {
  return spawnSync('git', ['-C', repoRoot, ...a], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
}

// 1. revision 是真实 commit 对象
if (/^[0-9a-fA-F]{40}$/.test(rev)) {
  const t = git('cat-file', '-t', rev);
  if (t.status !== 0) failures.push({ subject: '(repository)', problem: `本地对象库中不存在 revision ${rev}` });
  else if (t.stdout.trim() !== 'commit') failures.push({ subject: '(repository)', problem: `revision ${rev} 是 ${t.stdout.trim()}，不是 commit` });
}

// 2. 递归收集 sources：任何含 path(string)+line(number) 的对象视为一条证据引用；
//    所属容器 = 最近的带 string id 的祖先元素
const sources = [];
(function walk(node, container) {
  if (Array.isArray(node)) { for (const x of node) walk(x, container); return; }
  if (node && typeof node === 'object') {
    const nextContainer = typeof node.id === 'string' && node.id ? node.id : container;
    if (typeof node.path === 'string' && Number.isFinite(node.line)) {
      sources.push({
        container: nextContainer,
        path: node.path,
        line: Math.trunc(node.line),
        endLine: Number.isFinite(node.end_line) ? Math.trunc(node.end_line) : Math.trunc(node.line),
      });
    }
    for (const [k, v] of Object.entries(node)) {
      if (k === 'id') continue;
      walk(v, nextContainer);
    }
  }
})(ir, '(root)');

if (sources.length === 0) failures.push({ subject: '(root)', problem: 'IR 中未发现任何 sources 证据引用（含 path+line 的对象）' });

// 3. 逐条验证（每个文件的行数只算一次）
const lineCache = new Map();
function fileLineCount(p) {
  if (lineCache.has(p)) return lineCache.get(p);
  const show = git('show', `${rev}:${p}`);
  let total;
  if (show.status !== 0) total = -1;
  else {
    const content = show.stdout;
    total = content.length === 0 ? 0 : content.split('\n').length - (content.endsWith('\n') ? 1 : 0);
  }
  lineCache.set(p, total);
  return total;
}

for (const s of sources) {
  const subject = `${s.container} -> ${s.path}:${s.line}${s.endLine !== s.line ? '-' + s.endLine : ''}`;
  if (s.path.includes('..') || s.path.includes('\\') || path.isAbsolute(s.path) || s.path.startsWith('.git/')) {
    failures.push({ subject, problem: 'path 必须是仓库相对 POSIX 路径（禁 ..、反斜杠、绝对路径）' });
    continue;
  }
  const exists = git('cat-file', '-e', `${rev}:${s.path}`);
  if (exists.status !== 0) { failures.push({ subject, problem: `路径在 commit ${rev.slice(0, 12)} 中不存在` }); continue; }
  const total = fileLineCount(s.path);
  if (total < 0) { failures.push({ subject, problem: `git show 读取失败` }); continue; }
  if (s.line < 1) failures.push({ subject, problem: `line 必须 >= 1` });
  else if (s.endLine < s.line) failures.push({ subject, problem: `end_line ${s.endLine} < line ${s.line}` });
  else if (s.endLine > total) failures.push({ subject, problem: `行号越界：请求到第 ${s.endLine} 行，文件在 commit 内共 ${total} 行 → 修 end_line 为 ${total}` });
}

const ok = failures.length === 0;
// verified 计数：排除根级失败（'(root)' = 空 IR、'(repository)' = revision 问题），
// 只扣逐条 source 失败；下限 0——revision 验证失败时 sources.length 可能为 0，
// 直接相减会得 -1
const verifiedCount = Math.max(0,
  sources.length - failures.filter(f =>
    f.subject !== '(root)' && f.subject !== '(repository)').length);
const receipt = {
  ok,
  revision: rev,
  repoRoot,
  verified: verifiedCount,
  totalReferences: sources.length,
  failures,
};
console.log(JSON.stringify(receipt, null, 2));
process.exit(ok ? 0 : 1);
