# 间接寻址处理指南

## 背景

当前 skill 默认拒绝间接寻址的向量化请求。但在 `semantic_contract` 能证明索引、边界和别名语义时，SVE gather/scatter 可以作为受控路径；NEON 模拟 gather/scatter 只能作为低优先级草案或交给 memory-access-optimization，不默认视为成功优化。本文档说明如何判断和处理间接寻址场景。

## 拒绝 vs 可处理

### 必须拒绝的情况

以下情况必须直接拒绝，无法预处理：

1. **索引数组有重复元素** → 多个迭代可能写同一位置，存在真正的写冲突
2. **索引数组在循环内修改** → 无法预计算指针数组
3. **目标指针别名不确定** → `base` 可能与其他输入/输出重叠
4. **循环体有复杂操作** → 不是简单的 `*ptr += value` 或 `*ptr = value`
5. **要求严格顺序语义** → scatter/gather 可能改变写入顺序
6. **semantic_contract 缺失或只写 unknown** → 按无法证明处理

### 可预处理的情况

满足以下全部条件时可预处理后向量化：

1. **索引数组只读且无重复** → 每个迭代写唯一位置
2. **目标指针可预计算** → `ptrs[i] = base + indices[i]`
3. **无别名冲突** → `base` 与其他指针无重叠
4. **循环体简单** → 只有 load/store 或简单算术
5. **边界可证明** → `indices[i]` 不越界，或者 loop guard 明确保护

对于只读 gather 且写出是连续 `out[i]` 的场景，索引不重复不是 correctness 必需条件，但仍要证明索引只读、边界内、无别名冲突；对于 scatter 或 read-modify-write scatter，`unique` 是必需条件。

## 预处理策略

### 策略 1: 指针数组预计算

```c
// 原始间接更新
void scatter_accumulate(float *out, const int *indices, float value, int N) {
    for (int i = 0; i < N; i++) {
        int dst = indices[i];
        out[dst] += value;
    }
}

// 预处理方案（当 indices 无重复时）
void scatter_accumulate_vectorized(float *out, const int *indices, float value, int N) {
    // Step 1: 预计算指针数组（标量预处理）
    float *out_ptrs[N];
    for (int i = 0; i < N; i++) {
        out_ptrs[i] = out + indices[i];
    }
    
    // Step 2: 向量化更新
    // NEON: 由于指针不连续，无法直接向量化
    // 此方案主要用于 SVE gather/scatter
}
```

### 策略 2: SVE Gather/Scatter

SVE 提供原生 gather load 和 scatter store 支持：

```c
// SVE gather load + scatter store
#include <arm_sve.h>

void scatter_update_sve(float *out, const int *indices, float *values, int N) {
    int vl = svcntw();
    for (int i = 0; i < N; i += vl) {
        svbool_t pg = svwhilelt_b32(i, N);
        
        // Gather load indices
        svint32_t vidx = svld1sw_s32(pg, (const int32_t *)indices + i);
        
        // Gather load values
        svfloat32_t vval = svld1_f32(pg, values + i);
        
        // Gather load current values from scattered locations
        svfloat32_t vcur = svld1_gather_index_f32(pg, out, vidx);
        
        // Update
        vcur = svadd_f32_x(pg, vcur, vval);
        
        // Scatter store back
        svst1_scatter_index_f32(pg, out, vidx, vcur);
    }
}
```

**注意**: SVE scatter store 或 scatter read-modify-write 要求索引无重复，否则多个 lane 可能写同一位置，结果不再等价于标量顺序。

### 策略 3: NEON 模拟 Gather

NEON 无原生 gather 支持，需要标量辅助：

```c
// NEON: 逐元素标量辅助 + 向量化计算
void indirect_update_neon(float *out, const int *indices, 
                           const float *a, const float *b, int N) {
    // NEON 无法高效处理不连续访问
    // 若 indices 指向连续块，可尝试批量处理
    
    int i = 0;
    for (; i + 4 <= N; i += 4) {
        // 标量收集 4 个位置的值
        float vals[4];
        for (int j = 0; j < 4; j++) {
            vals[j] = out[indices[i + j]];
        }
        
        // 向量化计算
        float32x4_t vout = vld1q_f32(vals);
        float32x4_t va = vld1q_f32(a + i);
        float32x4_t vb = vld1q_f32(b + i);
        vout = vmlaq_f32(vout, va, vb);
        
        // 标量散开
        vst1q_f32(vals, vout);
        for (int j = 0; j < 4; j++) {
            out[indices[i + j]] = vals[j];
        }
    }
    
    // 尾处理
    for (; i < N; i++) {
        out[indices[i]] += a[i] * b[i];
    }
}
```

此方案效率取决于 `indices` 的分布。若分布随机，效率接近标量甚至更差。因此 NEON 模拟 gather 不能默认返回 `success=true`；只能作为未验证草案，或建议先交给 memory-access-optimization 做 AoS/SoA、tiling、重排或数据布局改造。

## 判断条件检查

在处理间接寻址请求时，模型应：

1. **检查索引数组是否只读**: 分析循环体是否修改 `indices[i]`
2. **检查索引是否可能重复**: scatter 写或 read-modify-write scatter 无法证明唯一时必须拒绝
3. **检查边界**: `indices[i]` 是否已由循环条件、预校验或 request 证明不越界
4. **检查别名**: 分析 `out` 是否可能与 `in`、`indices` 等重叠
5. **检查循环体复杂度**: 是否只有简单算术或赋值
6. **检查目标 ISA**: 只有 SVE gather/scatter 可以成为默认成功路径；NEON 模拟 gather 需要单独 benchmark 证明

### 可证明无重复的典型情况

- 索引是连续整数：`indices[i] = i` → 可直接向量化
- 紧凑映射：`indices[i] = i * stride` → 若 stride != 0，无重复
- 排序后的唯一值：已知排序且相邻差 != 0

### 无法证明时

必须返回 `success=false`，在 `safety_checks` 中说明：
- 无法证明索引数组无重复元素
- scatter 更新可能存在写冲突
- 建议用户提供额外约束信息

## request 语义契约

间接寻址 request 应使用 `semantic_contract`：

```json
{
  "semantic_contract": {
    "aliasing": "no_overlap",
    "index_properties": ["readonly", "unique", "in_bounds"],
    "math_mode": "strict",
    "requires_bit_exact": true,
    "allows_reassociation": false
  }
}
```

如果 `aliasing` 是 `unknown`，或者 scatter 场景缺少 `unique`，必须拒绝。模型不能把自然语言里的“应该不会重复”当作证明。

## response JSON 格式

### 预处理成功时

```json
{
  "vectorization_result": {
    "success": true,
    "original_loop": "...",
    "vectorized_code": "...",
    "safety_checks": [
      "已验证索引数组 indices 无重复元素",
      "已验证 out 不与其他指针别名",
      "使用 SVE scatter store，索引无重复确保正确性"
    ],
    "epilogue_handling": "使用 svwhilelt_b32 谓词处理尾元素"
  }
}
```

### 拒绝时

```json
{
  "vectorization_result": {
    "success": false,
    "original_loop": "...",
    "vectorized_code": "",
    "safety_checks": [
      "索引数组 indices 可能存在重复元素",
      "scatter 更新存在写冲突风险",
      "无法在当前信息下安全向量化"
    ],
    "error_message": "无法证明索引数组无重复，存在 scatter 写冲突风险，必须拒绝"
  }
}
```

## 相关文档

- `docs/arm-isa-usage-guide.md`: SVE gather/scatter intrinsics
- `docs/sme-za-inline-asm-guide.md`: SME ZA tile 间接访问
- `references/arm_intrinsics_db/sve.json`: gather/scatter intrinsics 列表
