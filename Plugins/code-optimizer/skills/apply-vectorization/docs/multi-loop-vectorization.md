# 多层嵌套循环分层向量化指南

## 背景

复杂算子通常包含多层嵌套循环。当前 skill 对多层循环的处理能力有限。本指南说明如何分析多层循环结构，识别可独立向量化的内层循环，实现分层向量化。

## 循环嵌套分析

### 循环依赖图

分析多层循环时，需要构建循环依赖图：

1. **识别循环层级**: 标记外层、内层循环及其迭代变量
2. **分析数据流**: 每层循环读取/写入的数据
3. **识别依赖类型**:
   - **循环内依赖**: 单次迭代内的数据流
   - **跨迭代依赖**: 迭代之间传递的值（如 accumulator）
   - **跨层依赖**: 外层循环变量影响内层循环

### 依赖类型判断

| 依赖类型 | 是否可向量化 | 原因 |
|---------|-------------|------|
| 循环内依赖 | ✅ 可以 | 数据在单次迭代内流动，可向量化 |
| 跨迭代依赖 | ❌ 不能 | 打破顺序会改变语义 |
| 跨层依赖（只读）| ✅ 可以 | 外层变量作为常量传入内层 |
| 跨层依赖（读写）| ❌ 不能 | 外层状态在内层修改 |

## 分层向量化策略

### 策略 1: 内层向量化，外层标量

最常用的分层策略：

```c
// 原始多层循环
void matrix_scale(float *out, const float *in, float scale, int rows, int cols) {
    for (int r = 0; r < rows; r++) {        // 外层：行遍历
        for (int c = 0; c < cols; c++) {    // 内层：列遍历
            out[r*cols + c] = in[r*cols + c] * scale;
        }
    }
}

// 分层向量化（SVE）
void matrix_scale_sve(float *out, const float *in, float scale, int rows, int cols) {
    for (int r = 0; r < rows; r++) {        // 外层保持标量
        float *row_out = out + r * cols;
        const float *row_in = in + r * cols;
        
        // 内层向量化
        int vl = svcntw();
        for (int c = 0; c < cols; c += vl) {
            svbool_t pg = svwhilelt_b32(c, cols);
            svfloat32_t vin = svld1_f32(pg, row_in + c);
            svfloat32_t vscale = svdup_f32(scale);
            vin = svmul_f32_x(pg, vin, vscale);
            svst1_f32(pg, row_out + c, vin);
        }
    }
}
```

**适用条件**:
- 内层循环无跨迭代依赖
- 内层循环计算密集
- 外层循环迭代次数少或无法向量化

### 策略 2: 行/列向双向量量化

当行和列都可向量化时：

```c
// NEON: 行向量 + 列向量双展开
void matrix_add_neon(float *out, const float *a, const float *b, 
                     int rows, int cols) {
    // 行方向展开 4 行
    int r = 0;
    for (; r + 4 <= rows; r += 4) {
        float *out0 = out + (r+0)*cols;
        float *out1 = out + (r+1)*cols;
        float *out2 = out + (r+2)*cols;
        float *out3 = out + (r+3)*cols;
        
        const float *a0 = a + (r+0)*cols;
        const float *a1 = a + (r+1)*cols;
        const float *a2 = a + (r+2)*cols;
        const float *a3 = a + (r+3)*cols;
        
        const float *b0 = b + (r+0)*cols;
        const float *b1 = b + (r+1)*cols;
        const float *b2 = b + (r+2)*cols;
        const float *b3 = b + (r+3)*cols;
        
        // 列方向向量化
        int c = 0;
        for (; c + 4 <= cols; c += 4) {
            float32x4_t va0 = vld1q_f32(a0 + c);
            float32x4_t va1 = vld1q_f32(a1 + c);
            float32x4_t va2 = vld1q_f32(a2 + c);
            float32x4_t va3 = vld1q_f32(a3 + c);
            
            float32x4_t vb0 = vld1q_f32(b0 + c);
            float32x4_t vb1 = vld1q_f32(b1 + c);
            float32x4_t vb2 = vld1q_f32(b2 + c);
            float32x4_t vb3 = vld1q_f32(b3 + c);
            
            vst1q_f32(out0 + c, vaddq_f32(va0, vb0));
            vst1q_f32(out1 + c, vaddq_f32(va1, vb1));
            vst1q_f32(out2 + c, vaddq_f32(va2, vb2));
            vst1q_f32(out3 + c, vaddq_f32(va3, vb3));
        }
        
        // 列方向尾处理
        for (; c < cols; c++) {
            out0[c] = a0[c] + b0[c];
            out1[c] = a1[c] + b1[c];
            out2[c] = a2[c] + b2[c];
            out3[c] = a3[c] + b3[c];
        }
    }
    
    // 行方向尾处理
    for (; r < rows; r++) {
        for (int c = 0; c < cols; c++) {
            out[r*cols + c] = a[r*cols + c] + b[r*cols + c];
        }
    }
}
```

**适用条件**:
- 行数和列数都可展开
- 内存带宽足够支持多行并行
- 无跨行依赖

### 策略 3: 计算分解

将复杂算子分解为多个可向量化的阶段：

```c
// Depthwise Convolution 分解
// 原始: 多层嵌套卷积
void depthwise_conv(float *out, const float *in, const float *weights,
                    int H, int W, int C, int KH, int KW) {
    for (int h = 0; h < H; h++) {
        for (int w = 0; w < W; w++) {
            for (int c = 0; c < C; c++) {
                float sum = 0;
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        sum += in[(h+kh)*W*C + (w+kw)*C + c] * weights[kh*KW*C + kw*C + c];
                    }
                }
                out[h*W*C + w*C + c] = sum;
            }
        }
    }
}

// 分解策略:
// 1. 外层 h, w 保持标量（空间遍历）
// 2. 内层 c 向量化（通道累加）
// 3. kh, kw 展开为标量乘累加序列

void depthwise_conv_vectorized(float *out, const float *in, const float *weights,
                                int H, int W, int C, int KH, int KW) {
    for (int h = 0; h < H; h++) {
        for (int w = 0; w < W; w++) {
            // 通道向量化累加
            int vl = svcntw();
            for (int c_start = 0; c_start < C; c_start += vl) {
                svbool_t pg = svwhilelt_b32(c_start, C);
                svfloat32_t vsum = svdup_f32(0.0f);
                
                // 展开 KH*KW 个乘累加
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        const float *in_ptr = in + (h+kh)*W*C + (w+kw)*C + c_start;
                        const float *w_ptr = weights + kh*KW*C + kw*C + c_start;
                        
                        svfloat32_t vin = svld1_f32(pg, in_ptr);
                        svfloat32_t vw = svld1_f32(pg, w_ptr);
                        vsum = svmla_f32_x(pg, vsum, vin, vw);
                    }
                }
                
                svst1_f32(pg, out + h*W*C + w*C + c_start, vsum);
            }
        }
    }
}
```

## 典型算子分析

### Pooling 2D

```c
// Pooling 结构分析
void avg_pool2d(float *out, const float *in, int H, int W, int C, int KH, int KW) {
    for (int h = 0; h < H; h++) {       // 输出行
        for (int w = 0; w < W; w++) {   // 输出列
            for (int c = 0; c < C; c++) {  // 通道 ← 可向量化
                float sum = 0;
                for (int kh = 0; kh < KH; kh++) {  // 窗口行
                    for (int kw = 0; kw < KW; kw++) {  // 窗口列
                        sum += in[(h*KH+kh)*W*C + (w*KW+kw)*C + c];
                    }
                }
                out[h*W*C + w*C + c] = sum / (KH * KW);
            }
        }
    }
}

// 向量化策略: c 层向量化，kh/kw 展开
void avg_pool2d_sve(float *out, const float *in, int H, int W, int C, 
                     int KH, int KW) {
    float scale = 1.0f / (KH * KW);
    int vl = svcntw();
    
    for (int h = 0; h < H; h++) {
        for (int w = 0; w < W; w++) {
            for (int c_start = 0; c_start < C; c_start += vl) {
                svbool_t pg = svwhilelt_b32(c_start, C);
                svfloat32_t vsum = svdup_f32(0.0f);
                
                for (int kh = 0; kh < KH; kh++) {
                    for (int kw = 0; kw < KW; kw++) {
                        svfloat32_t vin = svld1_f32(pg, 
                            in + (h*KH+kh)*W*C + (w*KW+kw)*C + c_start);
                        vsum = svadd_f32_x(pg, vsum, vin);
                    }
                }
                
                svst1_f32(pg, out + h*W*C + w*C + c_start, 
                          svmul_f32_x(pg, vsum, svdup_f32(scale)));
            }
        }
    }
}
```

### GEMV (向量-矩阵乘)

```c
// GEMV: y = A * x
void gemv(float *y, const float *A, const float *x, int M, int N) {
    for (int m = 0; m < M; m++) {       // 输出向量维度 ← 可向量化
        float sum = 0;
        for (int n = 0; n < N; n++) {   // 累加维度 ← 可向量化
            sum += A[m*N + n] * x[n];
        }
        y[m] = sum;
    }
}

// 策略 1: 内层 n 向量化（dot reduction）
void gemv_inner_vectorized(float *y, const float *A, const float *x, 
                            int M, int N) {
    for (int m = 0; m < M; m++) {
        const float *row = A + m * N;
        
        // NEON dot reduction
        float32x4_t vsum = vdupq_n_f32(0);
        int n = 0;
        for (; n + 4 <= N; n += 4) {
            float32x4_t va = vld1q_f32(row + n);
            float32x4_t vx = vld1q_f32(x + n);
            vsum = vmlaq_f32(vsum, va, vx);
        }
        
        // 水平归约
        float sum = vaddvq_f32(vsum);
        for (; n < N; n++) {
            sum += row[n] * x[n];
        }
        y[m] = sum;
    }
}

// 策略 2: 外层 m 向量化（多行并行）
void gemv_outer_vectorized(float *y, const float *A, const float *x,
                            int M, int N) {
    int m = 0;
    for (; m + 4 <= M; m += 4) {
        float32x4_t vy0 = vdupq_n_f32(0);
        float32x4_t vy1 = vdupq_n_f32(0);
        float32x4_t vy2 = vdupq_n_f32(0);
        float32x4_t vy3 = vdupq_n_f32(0);
        
        const float *row0 = A + (m+0)*N;
        const float *row1 = A + (m+1)*N;
        const float *row2 = A + (m+2)*N;
        const float *row3 = A + (m+3)*N;
        
        for (int n = 0; n < N; n += 4) {
            float32x4_t vx = vld1q_f32(x + n);
            
            vy0 = vmlaq_f32(vy0, vld1q_f32(row0 + n), vx);
            vy1 = vmlaq_f32(vy1, vld1q_f32(row1 + n), vx);
            vy2 = vmlaq_f32(vy2, vld1q_f32(row2 + n), vx);
            vy3 = vmlaq_f32(vy3, vld1q_f32(row3 + n), vx);
        }
        
        y[m+0] = vaddvq_f32(vy0);
        y[m+1] = vaddvq_f32(vy1);
        y[m+2] = vaddvq_f32(vy2);
        y[m+3] = vaddvq_f32(vy3);
    }
    
    // 尾处理
    for (; m < M; m++) {
        // ...标量处理
    }
}
```

## 判断流程

处理多层循环请求时：

1. **绘制循环结构图**: 标记每层循环的迭代变量和范围
2. **识别计算密集层**: 哪层的迭代次数最多、计算最重
3. **检查依赖**: 哪层有跨迭代依赖，哪层可独立
4. **选择向量化层**: 优先向量化计算密集且无依赖的层
5. **设计尾处理**: 向量化层 + 标量尾

### response JSON 格式

```json
{
  "vectorization_result": {
    "success": true,
    "safety_checks": [
      "识别为三层嵌套循环: h(行), w(列), c(通道)",
      "h, w 层跨迭代依赖（空间遍历），保持标量",
      "c 层无跨迭代依赖，计算密集，选择向量化",
      "KH*KW 窗口展开为标量乘累加序列"
    ],
    "epilogue_handling": "c 层使用 svwhilelt_b32 处理尾通道"
  }
}
```

## 相关文档

- `docs/operator-patterns.md`: 典型算子向量化模式
- `docs/reduction-guide.md`: reduction 向量化规则
- `docs/neon-asm-patterns.md`: 汇编级循环展开