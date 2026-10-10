//! 待优化 kernel：naive baseline（两遍 + 堆分配中间 Vec）。
//!
//! API 契约（bench 经 `mod kernel;` 调用，签名不可变）：
//! `pub fn weighted_positivity_score(a: &[f64], b: &[f64]) -> f64`
//! 语义：v = a[i]*(b[i]-0.5)+0.25，仅累加 v > 0 的项。

// 显式两遍 + 堆分配中间 Vec：filter+map collect 再单独求和（优化目标形态）。
pub fn weighted_positivity_score(a: &[f64], b: &[f64]) -> f64 {
    let positives: Vec<f64> = a
        .iter()
        .zip(b.iter())
        .filter(|(x, y)| {
            let v = **x * (**y - 0.5) + 0.25;
            v > 0.0
        })
        .map(|(x, y)| *x * (*y - 0.5) + 0.25)
        .collect(); // 中间 Vec：一次全量分配 + 写回 + 再读
    positives.iter().sum()
}
