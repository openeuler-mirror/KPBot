# 宽度跳转表模式

## 背景

处理不同宽度（4/8/16/32）的数据时，避免在循环内判断宽度可提升性能。参考 dav1d mc.S 的跳转表模式，本文档说明如何实现宽度自适应的高效处理。

## 问题分析

### 传统方式

```c
// 方式 1: 循环内判断（低效）
void process_width_in_loop(float *out, const float *in, int width, int n) {
    for (int i = 0; i < n; i++) {
        if (width == 4) {
            // 处理 4 元素
        } else if (width == 8) {
            // 处理 8 元素
        } else if (width == 16) {
            // 处理 16 元素
        } else {
            // 栄量处理
        }
    }
}
// 问题: 每次迭代都判断，分支预测失败率高
```

### 跳转表方式

```c
// 方式 2: 跳转表（高效）
void process_width_dispatch(float *out, const float *in, int width, int n) {
    // 根据宽度选择处理函数
    switch (width) {
        case 4:  process_4(out, in, n);  break;
        case 8:  process_8(out, in, n);  break;
        case 16: process_16(out, in, n); break;
        case 32: process_32(out, in, n); break;
        default: process_scalar(out, in, width, n);
    }
}
// 优势: 只在入口判断一次，循环内无分支
```

## 跳转表实现

### C 函数指针表

```c
typedef void (*process_func_t)(float *out, const float *in, int n);

// 处理函数表
static const process_func_t width_handlers[] = {
    process_4,   // width = 4
    process_8,   // width = 8
    process_16,  // width = 16
    process_32,  // width = 32
};

void process_width_table(float *out, const float *in, int width, int n) {
    // 计算表索引: width/4 - 1 (假设 width 是 4 的幂次)
    int idx = (width >> 2) - 1;
    if (idx >= 0 && idx < 4) {
        width_handlers[idx](out, in, n);
    } else {
        process_scalar(out, in, width, n);
    }
}
```

### 汇编跳转表（参考 dav1d）

```c
// 内联汇编跳转表
void process_width_asm_dispatch(float *out, const float *in, int width, int n) {
    // 使用 clz 计算宽度对应的跳转偏移
    __asm__ __volatile__(
        "clz w0, %w[width]\n"       // 计算前导零
        "sub w0, w0, #26\n"         // 调整偏移 (width=4 -> clz=30, idx=4)
        
        "adr x1, dispatch_table\n"
        "ldr w2, [x1, w0, lsl #2]\n"  // 加载跳转偏移
        "add x1, x1, w2\n"
        "br x1\n"                    // 跳转到处理函数
        
        ".align 4\n"
        "dispatch_table:\n"
        ".word handler_32 - dispatch_table\n"  // width = 32
        ".word handler_16 - dispatch_table\n"  // width = 16
        ".word handler_8  - dispatch_table\n"  // width = 8
        ".word handler_4  - dispatch_table\n"  // width = 4
        
        "handler_4:\n"
        "b process_4_asm\n"
        
        "handler_8:\n"
        "b process_8_asm\n"
        
        "handler_16:\n"
        "b process_16_asm\n"
        
        "handler_32:\n"
        "b process_32_asm\n"
        
        : 
        : [width] "r" (width)
        : "x0", "x1", "x2"
    );
}
```

## NEON 宽度处理器

### 4 元素处理

```c
void process_4_neon(float *out, const float *in, int n) {
    for (int i = 0; i < n; i++) {
        float32x4_t v = vld1q_f32(in + i * 4);
        // 处理...
        vst1q_f32(out + i * 4, v);
    }
}
```

### 8 元素处理

```c
void process_8_neon(float *out, const float *in, int n) {
    for (int i = 0; i < n; i++) {
        float32x4_t v0 = vld1q_f32(in + i * 8);
        float32x4_t v1 = vld1q_f32(in + i * 8 + 4);
        // 处理...
        vst1q_f32(out + i * 8, v0);
        vst1q_f32(out + i * 8 + 4, v1);
    }
}
```

### 16 元素处理

```c
void process_16_neon(float *out, const float *in, int n) {
    for (int i = 0; i < n; i++) {
        float32x4_t v[4];
        v[0] = vld1q_f32(in + i * 16);
        v[1] = vld1q_f32(in + i * 16 + 4);
        v[2] = vld1q_f32(in + i * 16 + 8);
        v[3] = vld1q_f32(in + i * 16 + 12);
        // 处理...
        vst1q_f32(out + i * 16, v[0]);
        vst1q_f32(out + i * 16 + 4, v[1]);
        vst1q_f32(out + i * 16 + 8, v[2]);
        vst1q_f32(out + i * 16 + 12, v[3]);
    }
}
```

### 32 元素处理

```c
void process_32_neon(float *out, const float *in, int n) {
    for (int i = 0; i < n; i++) {
        float32x4x4_t v = vld4q_f32(in + i * 32);
        // 处理...
        vst4q_f32(out + i * 32, v);
    }
}
```

## SVE 长度无关处理

SVE 通过谓词实现长度无关处理，无需跳转表：

```c
void process_width_sve(float *out, const float *in, int width, int n) {
    int vl = svcntw();  // 获取向量长度
    
    for (int i = 0; i < n; i++) {
        int j = 0;
        for (; j + vl <= width; j += vl) {
            svbool_t pg = svptrue_b32();  // 全活跃
            svfloat32_t v = svld1_f32(pg, in + i * width + j);
            // 处理...
            svst1_f32(pg, out + i * width + j, v);
        }
        
        // 尾处理
        if (j < width) {
            svbool_t pg = svwhilelt_b32(j, width);
            svfloat32_t v = svld1_f32(pg, in + i * width + j);
            // 处理...
            svst1_f32(pg, out + i * width + j, v);
        }
    }
}
```

## 多宽度混合处理

### 宽度阈值选择

```c
// 根据宽度选择不同策略
void process_adaptive(float *out, const float *in, int width, int n) {
    if (width <= 4) {
        // 标量或单向量处理
        process_4(out, in, n);
    } else if (width <= 8) {
        // 双向量处理
        process_8(out, in, n);
    } else if (width <= 16) {
        // 四向量处理
        process_16(out, in, n);
    } else {
        // 循环处理（宽度不固定）
        process_loop(out, in, width, n);
    }
}
```

### 混合宽度场景

```c
// 处理不同宽度的行（参考 dav1d bidir_fn）
void process_varied_widths(float *out, const float *in, 
                           const int *widths, int n_rows) {
    for (int r = 0; r < n_rows; r++) {
        int width = widths[r];
        int idx = width_to_index(width);
        width_handlers[idx](out, in, width);
    }
}
```

## 汇编模板参考

### dav1d mc.S 模式摘要

```asm
// dav1d 的跳转表结构（简化）
function avg_8bpc_neon, export=1
        clz             w4,  w4           // 计算前导零确定宽度
        movrel          x7, avg_tbl       // 加载跳转表地址
        sub             w4,  w4,  #24     // 调整偏移
        ldrsw           x4,  [x7, x4, lsl #2]
        avg             v4,  v0,  v1,  v2, v3  // 执行处理宏
        add             x7,  x7,  x4
        br              x7                // 跳转到宽度处理器

40:     // width = 4 处理器
        st1 {v4.s}[0], [x0], x1
        st1 {v4.s}[1], [x7], x1
        ...
        ret

80:     // width = 8 处理器
        st1 {v4.8b}, [x0], x1
        ...
        ret

160:    // width = 16 处理器
        st1 {v4.16b}, [x0], x1
        ...
        ret

320:    // width = 32 处理器
        st1 {v4.16b}, [x0], x1
        st1 {v5.16b}, [x0], x1
        ...
        ret

avg_tbl:
        .word 40b - avg_tbl   // offset for width = 4
        .word 80b - avg_tbl   // offset for width = 8
        .word 160b - avg_tbl  // offset for width = 16
        .word 320b - avg_tbl  // offset for width = 32
endfunc
```

**关键点**:
1. `clz` 计算宽度的幂次
2. 跳转表存储相对偏移（而非绝对地址）
3. 每个宽度处理器独立优化

## response JSON 格式

使用宽度跳转表时的 response：

```json
{
  "vectorization_result": {
    "success": true,
    "vectorized_code": "...",
    "safety_checks": [
      "使用宽度跳转表处理 4/8/16/32 元素",
      "入口 clz 计算索引，循环内无分支",
      "尾处理使用标量 fallback"
    ],
    "epilogue_handling": "宽度不匹配跳转表时使用标量处理"
  }
}
```

## 相关文档

- `docs/neon-asm-patterns.md`: NEON 汇编模板
- `docs/operator-patterns.md`: 算子模式（包含宽度选择逻辑）
- `docs/sme-za-inline-asm-guide.md`: SME ZA tile 处理