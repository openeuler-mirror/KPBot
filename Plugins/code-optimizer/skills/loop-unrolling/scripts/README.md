# 循环展开优化Skill - 辅助脚本说明

本目录包含循环展开优化Skill使用的辅助脚本。

## 脚本列表

### 1. detect_microarchitecture.py
微架构信息探测脚本

**功能**：
- 检测CPU微架构型号
- 检测流水线发射宽度
- 检测向量流水线数量
- 检测支持的向量位宽（128/256/512位）
- 检测CPU缓存层级与大小（L1/L2/L3）
- 检测支持的指令集（AVX/AVX2/AVX-512/NEON等）

**使用方法**：
```bash
python3 detect_microarchitecture.py
```

**输出**：
```json
{
  "cpu_model": "Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz",
  "vendor_id": "GenuineIntel",
  "pipeline_width": 4,
  "vector_pipelines": 2,
  "vector_width_bits": 256,
  "vector_width_bytes": 32,
  "supports_avx": true,
  "supports_avx2": true,
  "supports_avx512": false,
  "cache": {
    "l1_size_kb": 32,
    "l2_size_kb": 256,
    "l3_size_kb": 12288
  }
}
```

### 2. calculate_unroll_factor.py
计算最优循环展开系数

**功能**：
- 基于微架构信息计算最优展开系数
- 应用公式：最优展开系数 = 向量流水线数量 × 流水线发射宽度 × 缓存友好系数
- 确保展开系数是2的幂且不超过16

**使用方法**：
```bash
# 从标准输入读取微架构信息
python3 calculate_unroll_factor.py < microarchitecture.json

# 或从文件读取
python3 calculate_unroll_factor.py microarchitecture.json
```

**输出**：
```json
{
  "unroll_factor": 8,
  "calculation_logic": {
    "pipeline_width": 4,
    "vector_pipelines": 2,
    "l1_cache_size_kb": 32,
    "cache_coefficient": 1.0,
    "raw_unroll_factor": 8.0,
    "nearest_power_of_two": 8,
    "final_unroll_factor": 8,
    "formula": "2 × 4 × 1.0 = 8.0",
    "explanation": [...]
  }
}
```

## 完整工作流程示例

```bash
# 1. 检测微架构信息
python3 detect_microarchitecture.py

# 2. 计算最优展开系数
python3 detect_microarchitecture.py | python3 calculate_unroll_factor.py
```

## 依赖项

- Python 3.6+

## 许可证

这些脚本是循环展开优化Skill的一部分，遵循相同的许可证。
