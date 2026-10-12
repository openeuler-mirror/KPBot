//! bench/main.rs：独立参照 + 计时 harness（不信任被测 kernel）。
//! 参照用 f64 逐元素累加（保序，与 baseline 语义一致）；
//! rel_err 阈值 1e-3（精度变换契约：整数域精确，末尾单点 widen，
//! 实测 bit-exact；阈值留裕量容纳合法的累加顺序重排）。
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
        self.0 >> 33 // 高 31 位，取绝对值域
    }
}

fn gen_i8(n: usize, seed: u64) -> Vec<i8> {
    let mut rng = Lcg(seed);
    (0..n).map(|_| ((rng.next() & 0xff) as u8) as i8).collect()
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

    let q = gen_i8(n, 0x1234_5678_9abc_def0);
    let w = gen_i8(n, 0x0fed_cba9_8765_4321);

    // 独立标量参照（f64 单遍累加，写法与被测实现不同）
    let mut ref_sum = 0.0f64;
    for i in 0..n {
        ref_sum += (q[i] as f64) * (w[i] as f64);
    }

    let got = kernel::quant_dot(&q, &w);
    let rel_err = (got - ref_sum).abs() / ref_sum.abs().max(1e-12);

    let mut sink = 0.0f64;
    for _ in 0..2 {
        sink += kernel::quant_dot(&q, &w);
    }
    let t0 = Instant::now();
    for _ in 0..iters {
        sink += kernel::quant_dot(&q, &w);
    }
    let time_ms = t0.elapsed().as_secs_f64() * 1000.0 / iters as f64;

    let ok = rel_err < 1e-3;
    println!("CORRECT={}", ok as i32);
    println!("REL_ERR={:.6e}", rel_err);
    println!("TIME_MS={:.6}", time_ms);
    println!("SINK={:.6}", sink);
    std::process::exit(if ok { 0 } else { 1 });
}
