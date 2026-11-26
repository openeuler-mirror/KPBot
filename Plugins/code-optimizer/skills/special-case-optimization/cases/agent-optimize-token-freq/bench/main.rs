//! bench/main.rs：独立参照 + 计时 harness（不信任被测 kernel）。
//! 参照用与两臂都不同的第三写法（排序后游程计数），进一步隔离共性 bug。
//! 键值输出：CORRECT / REL_ERR / TIME_MS / SINK；exit code = 正确性。

mod kernel;

use std::time::Instant;

struct Lcg(u64);
impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

fn gen_tokens(n: usize, vocab: u32, seed: u64) -> Vec<u32> {
    let mut rng = Lcg(seed);
    (0..n).map(|_| (rng.next() % vocab as u64) as u32).collect()
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
    let vocab: usize = std::env::args()
        .nth(3)
        .and_then(|s| s.parse().ok())
        .unwrap_or(1024);

    let tokens = gen_tokens(n, vocab as u32, 0x1234_5678_9abc_def0);

    // 独立参照：排序 + 游程计数（第三种写法）
    let mut sorted = tokens.clone();
    sorted.sort_unstable();
    let threshold = (vocab as u64) / 2;
    let mut ref_count = 0u64;
    let mut i = 0usize;
    while i < sorted.len() {
        let v = sorted[i];
        let mut run = 0u64;
        while i < sorted.len() && sorted[i] == v {
            run += 1;
            i += 1;
        }
        if run >= threshold {
            ref_count += 1;
        }
    }

    let got = kernel::hot_token_count(&tokens, vocab);
    // 整数精确比较：rel_err = |got-ref|/max(ref,1)
    let rel_err = ((got as f64) - (ref_count as f64)).abs() / (ref_count as f64).max(1.0);

    let mut sink = 0u64;
    for _ in 0..2 {
        sink += kernel::hot_token_count(&tokens, vocab);
    }
    let t0 = Instant::now();
    for _ in 0..iters {
        sink += kernel::hot_token_count(&tokens, vocab);
    }
    let time_ms = t0.elapsed().as_secs_f64() * 1000.0 / iters as f64;

    let ok = rel_err < 1e-9; // 整数计数必须精确相等
    println!("CORRECT={}", ok as i32);
    println!("REL_ERR={:.6e}", rel_err);
    println!("TIME_MS={:.6}", time_ms);
    println!("SINK={}", sink);
    std::process::exit(if ok { 0 } else { 1 });
}
