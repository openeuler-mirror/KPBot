//! 分析对象 kernel：标量点积（数据依赖链清晰，MCA 可归因到依赖瓶颈）。
//! 注意：函数放在 mod calc 内——容器内编译产出的 mangled 符号形如
//! _ZN4calc3dot17h<hash>E，用例验证 17h hash 容忍匹配正是依赖此形状。
pub mod calc {
    pub fn dot(a: &[f32], b: &[f32]) -> f32 {
        let mut s = 0.0f32;
        for i in 0..a.len() {
            s += a[i] * b[i];
        }
        s
    }
}
