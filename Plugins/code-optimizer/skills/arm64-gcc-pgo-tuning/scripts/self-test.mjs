#!/usr/bin/env node
// 小型逻辑自检：不创建实验，不构建项目，不生成业务训练数据。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { validate, within } from './lib/contract.mjs';
import { normalizeCommand, mapContainsArchive } from './lib/cmake.mjs';
import { compare } from './lib/statistics.mjs';

test('缺失输入必须报告，不能补默认条件', () => {
  const input = { version: 1 };
  assert.ok(validate(input).length > 10);
  assert.deepEqual(input, { version: 1 });
});
test('畸形输入不能通过契约校验', () => {
  for (const input of [null, [], { training: 'bad' }, { datasets: [null] }]) {
    assert.ok(validate(input).length > 0);
  }
});
test('路径边界不接受相同目录或同名前缀兄弟', () => {
  assert.equal(within('/a/b', '/a/b'), false);
  assert.equal(within('/a/b', '/a/b2'), false);
  assert.equal(within('/a/b', '/a/b/c'), true);
});
test('编译等价性只剔除 PGO 阶段参数', () => {
  assert.deepEqual(normalizeCommand('gcc -O3'), normalizeCommand('gcc -O3 -fprofile-use=/tmp/profile'));
  assert.notDeepEqual(normalizeCommand('gcc -O3'), normalizeCommand('gcc -Ofast -fprofile-use=/tmp/profile'));
});
test('链接证据不能误认同名外部库', () => {
  const build = path.resolve('build');
  assert.equal(mapContainsArchive('LOAD libx.a', build, 'libx.a'), true);
  assert.equal(mapContainsArchive('LOAD ../other/libx.a', build, 'libx.a'), false);
});
// 仅测试统计函数，数值并非用户实验默认参数。
const config = {
  measurement: { seed: 1, direction: 'lower', pairs: 6, bootstrap_samples: 1000, min_gain_pct: 1, max_cv_pct: 10 },
  validation: [{ id: 'sample', weight: 1, max_regression_pct: 5 }],
};
for (const [candidate, outcome] of [[90, 'improved'], [100, 'no_gain'], [120, 'regressed']]) {
  test(`统计判定 ${outcome}`, () => {
    const samples = { sample: Array.from({ length: 6 }, () => ({ B: 100, P: candidate })) };
    assert.equal(compare(config, samples).optimization_outcome, outcome);
  });
}
test('主文档的本地链接均可解析', () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  const file = path.join(root, 'SKILL.md');
  for (const [, link] of fs.readFileSync(file, 'utf8').matchAll(/\]\(([^)]+)\)/g)) {
    if (!/^https?:/.test(link)) assert.ok(fs.existsSync(path.resolve(root, link)));
  }
});
