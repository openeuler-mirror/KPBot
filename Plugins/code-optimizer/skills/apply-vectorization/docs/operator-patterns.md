# 常见算子向量化模式

## 背景

许多常见算子有固定的向量化模式。识别这些模式后，可直接应用对应的向量化策略，避免逐元素分析。本文档基于 eval_framework 多项目（ComputeLibrary、dav1d、x264）的实践总结。

## 模式识别

### 算子分类

| 类别 | 典型算子 | 核心计算模式 | 推荐向量化策略 |
|------|---------|-------------|---------------|
| 元素级 | scale, add, mul | 逐元素映射 | 直接向量化 |
| 窗口级 | pooling, conv | 窗口累加 | 内层通道向量化 |
| 向量级 | gemv, dot | 向量内积 | dot reduction 或多行并行 |
| 矩阵级 | gemm | 矩阵乘 | 分块 + tile 策略 |
| 像素级 | sad, ssd | 像素差累加 | vabdl + vabal |

### 下一阶段补充场景

这些场景不要全部塞进“普通逐元素 map”，需要单独识别和门控：

| 场景 | 典型模式 | 推荐策略 |
| --- | --- | --- |
| Strided / interleaved data | RGB/RGBA、I/Q、AoS 通道交错 | NEON `vld2/vld3/vld4`、`zip/unzip/trn`，或先转交 memory-access-optimization 做 SoA |
| Widen / narrow / saturating / rounding | 图像、DSP、量化 requantize | 使用 `vmovl/vqmovn/vqrdmulh/vrshrn` 等 widen/narrow/saturating 路径 |
| min/max/product reduction | 归约但非 sum/dot | 单独记录结合律、NaN/有符号零、溢出语义；不要套 sum 模板 |
| argmin / argmax | 同时输出值和索引 | 值归约与索引 tie-break 分离，严格定义相等时选择首个还是最后一个 |
| int8 dotprod / i8mm | int8 GEMM、卷积、量化内积 | 在 `isa_extensions` 明确 `dotprod` 或 `i8mm` 后使用专用指令 |
| BF16 / FP16 | 低精度推理和 GEMM | 必须确认目标 ISA 和精度/舍入契约，不默认替代 float32 |
| CRC / crypto | CRC、GHASH、AES/SHA 辅助 | 走 PMULL/crypto extension 或专用 skill，不当作普通 SIMD map |
| `expf/sinf/logf` 等数学函数 | 激活函数、信号处理 | 优先 compiler veclib、ArmPL/Libamath、SLEEF；无向量数学库时明确拒绝或保持标量 |
| Portable SIMD | 多平台库代码 | 可建议 Google Highway 等 portable SIMD 路径，作为独立模式而不是混入 ARM-only response |

## 元素级算子

### Scale / Add / Mul

```c
// 原始
void scale_array(float *out, const float *in, float s, int n) {
    for (int i = 0; i < n; i++) {
        out[i] = in[i] * s;
    }
}

// NEON
void scale_neon(float *out, const float *in, float s, int n) {
    float32x4_t vs = vdupq_n_f32(s);
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t v = vld1q_f32(in + i);
        v = vmulq_f32(v, vs);
        vst1q_f32(out + i, v);
    }
    for (; i < n; i++) {
        out[i] = in[i] * s;
    }
}

// SVE
void scale_sve(float *out, const float *in, float s, int n) {
    svfloat32_t vs = svdup_f32(s);
    int vl = svcntw();
    for (int i = 0; i < n; i += vl) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, in + i);
        v = svmul_f32_x(pg, v, vs);
        svst1_f32(pg, out + i, v);
    }
}
```

**识别特征**: 单层循环，`out[i] = in[i] OP scalar`

## 窗口级算子

### Pooling (MAX/AVG)

```c
// 原始 2D Average Pooling
void avg_pool2d(float *out, const float *in, 
                int OH, int OW, int C, int KH, int KW, int stride) {
    for (int h = 0; h < OH; h++) {
        for (int w = 0; w < OW; w++) {
            for (int c = 0; c < C; c++) {
                float sum = 0;
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        int ih = h * stride + kh;
                        int iw = w * stride + kw;
                        sum += in[ih * OW * C + iw * C + c];
                    }
                }
                out[h * OW * C + w * C + c] = sum / (KH * KW);
            }
        }
    }
}

// 向量化模式: c 层向量化，窗口展开
void avg_pool2d_sve(float *out, const float *in,
                     int OH, int OW, int C, int KH, int KW, int stride) {
    float scale = 1.0f / (KH * KW);
    int vl = svcntw();
    
    for (int h = 0; h < OH; h++) {
        for (int w = 0; w < OW; w++) {
            for (int c_start = 0; c_start < C; c_start += vl) {
                svbool_t pg = svwhilelt_b32(c_start, C);
                svfloat32_t vsum = svdup_f32(0.0f);
                
                // 展开 KH*KW 窗口
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        int ih = h * stride + kh;
                        int iw = w * stride + kw;
                        const float *ptr = in + ih * OW * C + iw * C + c_start;
                        svfloat32_t vin = svld1_f32(pg, ptr);
                        vsum = svadd_f32_x(pg, vsum, vin);
                    }
                }
                
                svfloat32_t vout = svmul_f32_x(pg, vsum, svdup_f32(scale));
                svst1_f32(pg, out + h * OW * C + w * C + c_start, vout);
            }
        }
    }
}

// MAX Pooling
void max_pool2d_sve(float *out, const float *in,
                     int OH, int OW, int C, int KH, int KW, int stride) {
    int vl = svcntw();
    
    for (int h = 0; h < OH; h++) {
        for (int w = 0; w < OW; w++) {
            for (int c_start = 0; c_start < C; c_start += vl) {
                svbool_t pg = svwhilelt_b32(c_start, C);
                svfloat32_t vmax = svdup_f32(-INFINITY);
                
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        int ih = h * stride + kh;
                        int iw = w * stride + kw;
                        svfloat32_t vin = svld1_f32(pg, in + ih*OW*C + iw*C + c_start);
                        vmax = svmax_f32_x(pg, vmax, vin);
                    }
                }
                
                svst1_f32(pg, out + h*OW*C + w*C + c_start, vmax);
            }
        }
    }
}
```

**识别特征**:
- 输出循环: h, w（空间）
- 窗口循环: kh, kw（累加）
- 通道循环: c（可向量化）
- 操作: sum/max + normalize

### Depthwise Convolution

```c
// 原始 Depthwise Conv
void depthwise_conv(float *out, const float *in, const float *weights,
                     int OH, int OW, int C, int KH, int KW) {
    for (int h = 0; h < OH; h++) {
        for (int w = 0; w < OW; w++) {
            for (int c = 0; c < C; c++) {  // 可向量化
                float sum = 0;
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        int ih = h + kh;
                        int iw = w + kw;
                        sum += in[ih*OW*C + iw*C + c] * weights[kh*KW*C + kw*C + c];
                    }
                }
                out[h*OW*C + w*C + c] = sum;
            }
        }
    }
}

// 向量化模式
void depthwise_conv_sve(float *out, const float *in, const float *weights,
                         int OH, int OW, int C, int KH, int KW) {
    int vl = svcntw();
    
    for (int h = 0; h < OH; h++) {
        for (int w = 0; w < OW; w++) {
            for (int c_start = 0; c_start < C; c_start += vl) {
                svbool_t pg = svwhilelt_b32(c_start, C);
                svfloat32_t vsum = svdup_f32(0.0f);
                
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        int ih = h + kh;
                        int iw = w + kw;
                        
                        svfloat32_t vin = svld1_f32(pg, 
                            in + ih*OW*C + iw*C + c_start);
                        svfloat32_t vw = svld1_f32(pg, 
                            weights + kh*KW*C + kw*C + c_start);
                        
                        vsum = svmla_f32_x(pg, vsum, vin, vw);
                    }
                }
                
                svst1_f32(pg, out + h*OW*C + w*C + c_start, vsum);
            }
        }
    }
}
```

**识别特征**:
- 每个输出通道对应一个输入通道（无跨通道计算）
- 权重形状与输入窗口相同
- 操作: 乘累加

## 向量级算子

### GEMV (矩阵-向量乘)

```c
// 原始: y = A * x
void gemv(float *y, const float *A, const float *x, int M, int N) {
    for (int m = 0; m < M; m++) {
        float sum = 0;
        for (int n = 0; n < N; n++) {
            sum += A[m*N + n] * x[n];
        }
        y[m] = sum;
    }
}

// 策略 1: 内层 n 向量化（dot reduction）
void gemv_dotprod(float *y, const float *A, const float *x, int M, int N) {
    for (int m = 0; m < M; m++) {
        float32x4_t vsum = vdupq_n_f32(0);
        const float *row = A + m * N;
        
        for (int n = 0; n + 4 <= N; n += 4) {
            float32x4_t va = vld1q_f32(row + n);
            float32x4_t vx = vld1q_f32(x + n);
            vsum = vmlaq_f32(vsum, va, vx);
        }
        
        y[m] = vaddvq_f32(vsum);  // 水平归约
        // 尾处理...
    }
}

// 策略 2: 外层 m 向量化（多行并行）
void gemv_parallel_rows(float *y, const float *A, const float *x, int M, int N) {
    int m = 0;
    for (; m + 4 <= M; m += 4) {
        float32x4_t vy[4] = {vdupq_n_f32(0)};
        const float *rows[4];
        for (int i = 0; i < 4; i++) rows[i] = A + (m+i) * N;
        
        for (int n = 0; n + 4 <= N; n += 4) {
            float32x4_t vx = vld1q_f32(x + n);
            for (int i = 0; i < 4; i++) {
                float32x4_t va = vld1q_f32(rows[i] + n);
                vy[i] = vmlaq_f32(vy[i], va, vx);
            }
        }
        
        for (int i = 0; i < 4; i++) {
            y[m+i] = vaddvq_f32(vy[i]);
        }
    }
    // 尾处理...
}
```

**识别特征**:
- 一个输入是向量（x），一个是矩阵（A）
- 输出是向量（y）
- 核心是 dot product

### Dot Product

```c
// 原始
float dot(const float *a, const float *b, int n) {
    float sum = 0;
    for (int i = 0; i < n; i++) {
        sum += a[i] * b[i];
    }
    return sum;
}

// NEON
float dot_neon(const float *a, const float *b, int n) {
    float32x4_t vsum = vdupq_n_f32(0);
    int i = 0;
    for (; i + 4 <= n; i += 4) {
        float32x4_t va = vld1q_f32(a + i);
        float32x4_t vb = vld1q_f32(b + i);
        vsum = vmlaq_f32(vsum, va, vb);
    }
    float sum = vaddvq_f32(vsum);
    for (; i < n; i++) sum += a[i] * b[i];
    return sum;
}

// SVE (长度无关)
float dot_sve(const float *a, const float *b, int n) {
    svfloat32_t vsum = svdup_f32(0);
    int vl = svcntw();
    for (int i = 0; i < n; i += vl) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t va = svld1_f32(pg, a + i);
        svfloat32_t vb = svld1_f32(pg, b + i);
        vsum = svmla_f32_x(pg, vsum, va, vb);
    }
    return svtmad_f32(vsum, 0.0f, 0);  // 或 svaddv_f32
}
```

**识别特征**: 双输入向量，单输出 scalar，`sum += a[i] * b[i]`

## 像素级算子

### SAD (Sum of Absolute Differences)

```c
// 原始
uint32_t sad8(const uint8_t *a, const uint8_t *b, int n) {
    uint32_t sum = 0;
    for (int i = 0; i < n; i++) {
        int diff = (int)a[i] - (int)b[i];
        sum += (diff < 0) ? -diff : diff;
    }
    return sum;
}

// NEON (参考 x264 pixel.S)
uint32_t sad8_neon(const uint8_t *a, const uint8_t *b, int n) {
    uint32x4_t vsum = vdupq_n_u32(0);
    int i = 0;
    for (; i + 8 <= n; i += 8) {
        uint8x8_t va = vld1_u8(a + i);
        uint8x8_t vb = vld1_u8(b + i);
        
        // vabdl: 计算绝对差并扩展到 16-bit
        uint16x8_t vdiff = vabdl_u8(va, vb);
        
        // vabal: 累加绝对差
        vsum = vabal_u16(vsum, vdiff);  // 需要 uint32x4_t accumulator
    }
    return vaddvq_u32(vsum);
}
```

**识别特征**: `sum += abs(a[i] - b[i])`

### SSD (Sum of Squared Differences)

```c
// 原始
uint32_t ssd8(const uint8_t *a, const uint8_t *b, int n) {
    uint32_t sum = 0;
    for (int i = 0; i < n; i++) {
        int diff = (int)a[i] - (int)b[i];
        sum += diff * diff;
    }
    return sum;
}

// NEON
uint32_t ssd8_neon(const uint8_t *a, const uint8_t *b, int n) {
    uint32x4_t vsum = vdupq_n_u32(0);
    int i = 0;
    for (; i + 8 <= n; i += 8) {
        int16x8_t va = vreinterpretq_s16_u16(vmovl_u8(vld1_u8(a + i)));
        int16x8_t vb = vreinterpretq_s16_u16(vmovl_u8(vld1_u8(b + i)));
        
        int16x8_t vdiff = vsubq_s16(va, vb);
        
        // vmlal: 扩展乘法并累加
        int32x4_t vprod = vmull_s16(vget_low_s16(vdiff), vget_low_s16(vdiff));
        vsum = vaddq_u32(vsum, vreinterpretq_u32_s32(vprod));
        
        int32x4_t vprod_hi = vmull_high_s16(vdiff, vdiff);
        vsum = vaddq_u32(vsum, vreinterpretq_u32_s32(vprod_hi));
    }
    return vaddvq_u32(vsum);
}
```

**识别特征**: `sum += (a[i] - b[i])^2`

## 模式匹配规则

### 判断流程

处理算子请求时：

1. **识别算子类别**: 匹配上述表格中的算子类型
2. **确认数据布局**: NHWC / NCHW / 行主序 / 列主序
3. **选择向量化层**: 确定哪层循环可向量化
4. **选择 ISA**: NEON / SVE / SME
5. **应用模板**: 使用对应算子的向量化模板

### response JSON 示例

```json
{
  "vectorization_result": {
    "success": true,
    "operator_pattern": "avg_pool2d",
    "safety_checks": [
      "识别为 2D Average Pooling 模式",
      "布局为 NHWC，通道维度 C 可向量化",
      "窗口 KH*KW 可展开为标量乘累加序列",
      "使用 SVE svwhilelt_b32 处理通道尾"
    ],
    "epilogue_handling": "通道循环使用谓词处理尾元素，无需标量尾循环"
  }
}
```

## Benchmark 经验结论

三路对比时必须同时看：禁用自动向量化的标量基线、编译器 `-O3` 自动向量化、显式 skill 生成结果。只和禁用自动向量化的标量版本比，容易高估简单 map 类循环的收益。

近期外部源码案例的可复用结论：

- 简单连续 bitwise map 往往会被 Clang 自动向量化得很好，显式 NEON 不一定明显胜出
- `AbsoluteDifference<uint8_t>` 这类可直接映射到专用 intrinsic 的模式更适合 skill 介入，例如 `vabdq_u8`
- JPEG downsample 这类固定相位、交替 bias 或短周期状态的循环，是显式向量化更容易超过自动向量化的场景
- 带 `expf()` 等复杂标量库调用的激活函数不应硬改成当前 skill 的基础向量化结果

## 相关文档

- `docs/multi-loop-vectorization.md`: 多层循环分析
- `docs/neon-asm-patterns.md`: NEON 汇编模板
- `docs/indirect-addressing-handling.md`: 间接寻址处理
