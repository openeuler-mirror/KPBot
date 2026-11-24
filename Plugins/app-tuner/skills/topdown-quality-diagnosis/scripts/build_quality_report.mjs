import {args, main, readJson, writeNew, escapeCell as cell} from './common.mjs';

export function report(result) {
  if (result.schema_version !== 1 || !Array.isArray(result.checks)) {
    throw new Error('Expected a quality result with schema_version=1');
  }
  const lines = [
    '# Top-down 采集质量审核',
    '',
    `运行：${cell(result.run_id)}`,
    '',
    `质量状态：**${cell(result.quality_status)}**。这是离线规则审核，不是实机认证。`,
    '',
    `原始证据：${cell(result.source?.path)}`,
    '',
    `SHA256：${cell(result.source?.sha256)}`,
    '',
    '## 已选指标',
    '',
    '| 指标 | 值 | 单位 | 原始行 |',
    '|---|---:|---|---:|',
  ];
  for (const [name, item] of Object.entries(result.metrics || {})) {
    lines.push(`| ${cell(name)} | ${cell(item.value)} | ${cell(item.unit)} | ${cell(item.line)} |`);
  }
  lines.push('', '这些值不自动成为可信结论；需结合下列质量检查解释。', '');
  lines.push(`每单位 cycles：${cell(result.cycles_per_unit)}；单位：${cell(result.cycles_per_unit_unit)}。`, '');
  lines.push('## 质量检查', '', '| 检查 | 状态 | 说明 | 证据 |', '|---|---|---|---|');
  for (const check of result.checks) {
    lines.push(`| ${cell(check.id)} | ${cell(check.status)} | ${cell(check.detail)} | ${cell(check.evidence)} |`);
  }
  lines.push('', '## 原始警告', '');
  if (!result.warnings?.length) lines.push('没有匹配到预设警告关键词；仍须阅读原始日志。');
  for (const warning of result.warnings || []) {
    lines.push(`- 行 ${cell(warning.line)}：${cell(warning.text)}`);
  }
  lines.push('', '## 比较与下一步', '', cell(result.comparison_reason), '');
  const pending = result.checks.filter(check => check.status !== 'pass');
  for (const check of pending) {
    lines.push(`- ${cell(check.id)}：补充证据或区分实验，解决后在新运行目录复核。`);
  }
  if (!pending.length) lines.push('单次规则通过；继续审核配对条件、重复性和业务指标。');
  lines.push('', '## 人工诊断与建议', '');
  lines.push('交付前补充范围解释、原因假设、复采变量、停止条件及验证结果。');
  lines.push('性能建议需引用可信证据；不从高百分比直接推导优化收益。');
  return lines.join('\n') + '\n';
}

main(import.meta.url, () => {
  const opt = args(process.argv.slice(2), ['input', 'out']);
  writeNew(opt.out, report(readJson(opt.input)));
});
