// 配对 bootstrap。单位是独立进程轮次；所有阈值来自冻结契约。
import { fail } from './contract.mjs';
const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;
const cv = (a) =>
  (Math.sqrt(a.reduce((s, x) => s + (x - mean(a)) ** 2, 0) / (a.length - 1)) / mean(a)) * 100;
function rng(seed) {
  let x = seed >>> 0;
  return () => {
    x = (Math.imul(x, 1664525) + 1013904223) >>> 0;
    return x / 4294967296;
  };
}
function quantile(a, p) {
  const v = [...a].sort((x, y) => x - y),
    i = (v.length - 1) * p,
    j = Math.floor(i);
  return v[j] + (v[Math.min(j + 1, v.length - 1)] - v[j]) * (i - j);
}
export function compare(c, samples) {
  const m = c.measurement,
    random = rng(m.seed),
    rows = [];
  const gain = (b, p) => (m.direction === 'higher' ? p / b - 1 : 1 - p / b);
  for (const s of c.validation) {
    const pairs = samples[s.id];
    if (
      !Array.isArray(pairs) ||
      pairs.length !== m.pairs ||
      pairs.some((x) => !Number.isFinite(x.B) || x.B <= 0 || !Number.isFinite(x.P) || x.P <= 0)
    )
      fail('invalid_measurements', `无效配对样本: ${s.id}`);
    const b = pairs.map((x) => x.B),
      p = pairs.map((x) => x.P),
      boots = [];
    for (let i = 0; i < m.bootstrap_samples; i++) {
      const draw = Array.from(
        { length: pairs.length },
        () => pairs[Math.floor(random() * pairs.length)],
      );
      boots.push(gain(mean(draw.map((x) => x.B)), mean(draw.map((x) => x.P))) * 100);
    }
    // Bonferroni 修正场景退化检查；综合指标另用 95% 区间。
    const alpha = 0.05 / (2 * c.validation.length);
    rows.push({
      id: s.id,
      weight: s.weight,
      gain_pct: gain(mean(b), mean(p)) * 100,
      interval_pct: [quantile(boots, alpha), quantile(boots, 1 - alpha)],
      baseline_mean: mean(b),
      candidate_mean: mean(p),
      cv_pct: Math.max(cv(b), cv(p)),
      max_regression_pct: s.max_regression_pct,
      boots,
    });
  }
  const total = rows.reduce((x, r) => x + r.weight, 0),
    overall = [];
  for (let i = 0; i < m.bootstrap_samples; i++)
    overall.push(rows.reduce((x, r) => x + (r.boots[i] * r.weight) / total, 0));
  const interval = [quantile(overall, 0.025), quantile(overall, 0.975)];
  let outcome = 'inconclusive';
  if (rows.some((r) => r.cv_pct > m.max_cv_pct)) outcome = 'inconclusive';
  else if (rows.some((r) => r.interval_pct[1] < -r.max_regression_pct) || interval[1] < 0)
    outcome = 'regressed';
  else if (
    interval[0] >= m.min_gain_pct &&
    rows.every((r) => r.interval_pct[0] >= -r.max_regression_pct)
  )
    outcome = 'improved';
  else if (interval[1] < m.min_gain_pct) outcome = 'no_gain';
  return {
    optimization_outcome: outcome,
    aggregate: 'user-weighted arithmetic mean of scenario percentage gains',
    gain_pct: rows.reduce((x, r) => x + (r.gain_pct * r.weight) / total, 0),
    interval_pct: interval,
    scenarios: rows.map(({ boots, ...r }) => r),
  };
}
