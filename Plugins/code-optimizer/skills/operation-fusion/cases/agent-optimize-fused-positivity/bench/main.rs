//! bench/main.rs：独立标量参照 + 计时 harness（不信任被测 kernel）。
//! 键值输出：CORRECT / REL_ERR / TIME_MS / SINK；exit code = 正确性。
//! 数据由确定性 LCG 生成，任意机器/容器可复现。

mod kernel;

use std::time::Instant;

struct Lcg(u64);
impl Lcg {
    fn next_f64(&mut self) -> f64 {
        // 64 位 LCG（Knuth MMIX 常数），确定性
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 11) as f64) / ((1u64 << 53) as f64)
    }
}

fn gen(n: usize, seed: u64) -> Vec<f64> {
    let mut rng = Lcg(seed);
    (0..n).map(|_| rng.next_f64()).collect()
}

fn main() {
    let n: usize = std::env::args()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(4_000_000);
    let iters: usize = std::env::args()
        .nth(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(20);

    let a = gen(n, 0x1234_5678_9abc_def0);
    let b = gen(n, 0x0fed_cba9_8765_4321);

    // 独立标量参照（单遍 for，与被测实现写法不同）
    let mut ref_sum = 0.0f64;
    for i in 0..n {
        let v = a[i] * (b[i] - 0.5) + 0.25;
        if v > 0.0 {
            ref_sum += v;
        }
    }

    let got = kernel::weighted_positivity_score(&a, &b);
    let rel_err = (got - ref_sum).abs() / ref_sum.abs().max(1e-12);

    // 预热 2 次 + iters 次平均，防止时钟/缓存冷启动偏差
    let mut sink = 0.0f64;
    for _ in 0..2 {
        sink += kernel::weighted_positivity_score(&a, &b);
    }
    let t0 = Instant::now();
    for _ in 0..iters {
        sink += kernel::weighted_positivity_score(&a, &b);
    }
    let time_ms = t0.elapsed().as_secs_f64() * 1000.0 / iters as f64;

    let ok = rel_err < 1e-9;
    println!("CORRECT={}", ok as i32);
    println!("REL_ERR={:.6e}", rel_err);
    println!("TIME_MS={:.6}", time_ms);
    println!("SINK={:.6}", sink);
    std::process::exit(if ok { 0 } else { 1 });
}
