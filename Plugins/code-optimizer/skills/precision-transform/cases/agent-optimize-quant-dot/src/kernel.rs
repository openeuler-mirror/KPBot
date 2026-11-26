//! 待优化 kernel：naive baseline（逐元素 widen + 中间 Vec 两遍）。
//!
//! API 契约（bench 经 `mod kernel;` 调用，签名不可变）：
//! `pub fn quant_dot(q: &[i8], w: &[i8]) -> f64`
//! 语义：sum(q[i] * w[i])，以 f64 返回（解码-加权点积，量化推理常见形态）。

// naive：每对元素 widen 成 f64、collect 中间 Vec、再单独求和。
// 每元素 2 次 int->float 转换在乘加依赖链上 + 一次全量堆分配 + 两遍遍历。
pub fn quant_dot(q: &[i8], w: &[i8]) -> f64 {
    let prods: Vec<f64> = q
        .iter()
        .zip(w.iter())
        .map(|(x, y)| (*x as f64) * (*y as f64))
        .collect(); // 中间 Vec：全量分配 + 写回 + 再读
    prods.iter().sum()
}
