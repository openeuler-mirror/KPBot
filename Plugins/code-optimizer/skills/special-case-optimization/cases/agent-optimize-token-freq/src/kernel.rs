//! 待优化 kernel：naive baseline（通用 HashMap，无快路径）。
//!
//! API 契约（bench 经 `mod kernel;` 调用，签名不可变）：
//! `pub fn hot_token_count(tokens: &[u32], vocab_size: usize) -> u64`
//! 前置条件：所有 token < vocab_size（调用方/bench 保证，函数内不检查）。
//! 语义：返回出现次数 >= vocab_size/2 的 token 种类数。

use std::collections::HashMap;

// naive：通用 HashMap 逐元素 entry+or_insert（每元素 hash + probe + rehash 风险）。
// vocab_size 是 public 参数但 baseline 完全没用它做分派 —— 快路径机会。
pub fn hot_token_count(tokens: &[u32], vocab_size: usize) -> u64 {
    let mut freq: HashMap<u32, u64> = HashMap::new();
    for t in tokens {
        *freq.entry(*t).or_insert(0) += 1;
    }
    let threshold = (vocab_size as u64) / 2;
    freq.values().filter(|c| **c >= threshold).count() as u64
}
