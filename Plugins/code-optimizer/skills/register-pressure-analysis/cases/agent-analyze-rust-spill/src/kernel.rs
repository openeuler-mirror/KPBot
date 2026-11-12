//! 分析对象 kernel：NEON 4x4 FMA micro-kernel（寄存器压力分析目标）。
//! 使用 core::arch::aarch64 intrinsic（rust_intrinsics source_kind）。
#![allow(clippy::too_many_arguments)]
pub mod microkernel {
    pub unsafe fn fma_4x4(
        a0: &[core::arch::aarch64::float32x4_t; 4],
        b: &[core::arch::aarch64::float32x4_t; 4],
        c: &mut [core::arch::aarch64::float32x4_t; 4],
    ) {
        use core::arch::aarch64::*;
        c[0] = vfmaq_f32(c[0], a0[0], b[0]);
        c[1] = vfmaq_f32(c[1], a0[1], b[1]);
        c[2] = vfmaq_f32(c[2], a0[2], b[2]);
        c[3] = vfmaq_f32(c[3], a0[3], b[3]);
    }
    pub fn fma_4x4_safe(a: &[[f32; 4]; 4], b: &[[f32; 4]; 4], c: &mut [[f32; 4]; 4]) {
        // 注：`[f32;4].into() -> float32x4_t` 的 From 实现不稳定（1.85 仍未提供），
        // 按设计文档 fallback 用 core::mem::transmute 等价转换（[f32;4] 与
        // float32x4_t 布局相同：4×f32、无 padding，repr 透明）。
        unsafe {
            let av: [core::arch::aarch64::float32x4_t; 4] = core::mem::transmute(*a);
            let bv: [core::arch::aarch64::float32x4_t; 4] = core::mem::transmute(*b);
            let mut cv: [core::arch::aarch64::float32x4_t; 4] = core::mem::transmute(*c);
            fma_4x4(&av, &bv, &mut cv);
            *c = core::mem::transmute(cv);
        }
    }
}
