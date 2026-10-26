//! bench/main.rs：正确性参照（确定性 LCG 数据 + 标量 FMA 4x4 参照 + 键值输出）。
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

fn gen_tile(seed: u64) -> [[f32; 4]; 4] {
    let mut rng = Lcg(seed);
    [
        [rng.next_f32(), rng.next_f32(), rng.next_f32(), rng.next_f32()],
        [rng.next_f32(), rng.next_f32(), rng.next_f32(), rng.next_f32()],
        [rng.next_f32(), rng.next_f32(), rng.next_f32(), rng.next_f32()],
        [rng.next_f32(), rng.next_f32(), rng.next_f32(), rng.next_f32()],
    ]
}

fn main() {
    let n: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(8192);
    // 生成 n 组 a/b tile（fma_4x4 语义：c[j][l] += a[j][m]*b[m][l]）
    let a_rows: Vec<[[f32; 4]; 4]> =
        (0..n).map(|i| gen_tile(0x1234_5678_9abc_def0u64.wrapping_add(i as u64))).collect();
    let b_tiles: Vec<[[f32; 4]; 4]> =
        (0..n).map(|i| gen_tile(0x0fed_cba9_8765_4321u64.wrapping_add(i as u64))).collect();

    // 标量参照：手写 FMA 4x4 tile（c[j][l] += a[j][m]*b[m][l]）
    let scalar_ref = |a: &[[f32; 4]; 4], b: &[[f32; 4]; 4]| -> [[f32; 4]; 4] {
        let mut c = [[0.0f32; 4]; 4];
        for j in 0..4usize {
            for l in 0..4usize {
                for m in 0..4usize {
                    c[j][l] += a[j][m] * b[m][l];
                }
            }
        }
        c
    };

    let mut max_rel = 0.0f64;
    let mut c_got_last: [[f32; 4]; 4] = [[0.0f32; 4]; 4];
    for i in 0..n {
        let expect = scalar_ref(&a_rows[i], &b_tiles[i]);
        let mut got = [[0.0f32; 4]; 4];
        kernel::microkernel::fma_4x4_safe(&a_rows[i], &b_tiles[i], &mut got);
        for j in 0..4usize {
            for l in 0..4usize {
                let e = ((got[j][l] - expect[j][l]) as f64).abs()
                    / (expect[j][l] as f64).abs().max(1e-12);
                if e > max_rel {
                    max_rel = e;
                }
            }
        }
        c_got_last = got;
    }

    let t0 = Instant::now();
    let mut sink: f32 = 0.0;
    for i in 0..n {
        let mut got = [[0.0f32; 4]; 4];
        kernel::microkernel::fma_4x4_safe(&a_rows[i], &b_tiles[i], &mut got);
        sink += got[0][0];
    }
    let time_ms = t0.elapsed().as_secs_f64() * 1000.0;

    println!("CORRECT={}", (max_rel < 1e-5) as i32);
    println!("REL_ERR={:.6e}", max_rel);
    println!("TIME_MS={:.6}", time_ms);
    println!("SINK={:.6}", sink + c_got_last[0][0]);
    std::process::exit(if max_rel < 1e-5 { 0 } else { 1 });
}
