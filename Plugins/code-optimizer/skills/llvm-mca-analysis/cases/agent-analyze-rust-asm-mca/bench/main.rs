//! bench/main.rs：正确性参照（确定性 LCG 数据 + 标量参照 + 键值输出）。
//! 分析类用例不跑 speedup，此 harness 只保证 kernel 语义正确（编译即验证）。
use std::time::Instant;

mod kernel;

struct Lcg(u64);
impl Lcg {
    fn next_f32(&mut self) -> f32 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 11) as f64 / (1u64 << 53) as f64) as f32
    }
}

fn gen(n: usize, seed: u64) -> Vec<f32> {
    let mut rng = Lcg(seed);
    (0..n).map(|_| rng.next_f32()).collect()
}

fn main() {
    let n: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(8192);
    let a = gen(n, 0x1234_5678_9abc_def0);
    let b = gen(n, 0x0fed_cba9_8765_4321);
    let mut ref_sum = 0.0f32;
    for i in 0..n { ref_sum += a[i] * b[i]; }
    let got = kernel::calc::dot(&a, &b);
    let rel_err = (got - ref_sum).abs() / ref_sum.abs().max(1e-12);
    let t0 = Instant::now();
    let _ = kernel::calc::dot(&a, &b);
    let time_ms = t0.elapsed().as_secs_f64() * 1000.0;
    println!("CORRECT={}", (rel_err < 1e-5) as i32);
    println!("REL_ERR={:.6e}", rel_err);
    println!("TIME_MS={:.6}", time_ms);
    println!("SINK={:.6}", got);
    std::process::exit(if rel_err < 1e-5 { 0 } else { 1 });
}
