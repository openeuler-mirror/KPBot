---
name: kunpeng-microarch
description: >
  Kunpeng-0xd01/0xd03/0xd06 微架构知识库 — 提供流水线结构、缓存层级、指令延迟/吞吐、
  功能单元分布、分支预测、SIMD 执行宽度等参考数据。当优化 C/C++ 代码需要了解
  Kunpeng 微架构特性时使用此 Skill。触发：提到 Kunpeng 微架构、鲲鹏微架构、
  Kunpeng-0xd01/0xd03/0xd06、0xd01/0xd03/0xd06、pipeline structure、
  cache hierarchy、instruction latency 等关键词。
---

# Kunpeng 微架构知识库

本 Skill 提供鲲鹏系列处理器的微架构参考数据，供代码优化决策使用。

## 支持的处理器

| 型号 | 代号 | 文档 |
|------|------|------|
| Kunpeng-0xd01 | TSV110 / 0xd01 | [kunpeng920-microarchitecture.md](kunpeng920-microarchitecture.md) |
| Kunpeng-0xd03 | 0xd03 | [kunpeng_uarch_b-microarchitecture.md](kunpeng_uarch_b-microarchitecture.md) |
| Kunpeng-0xd06 | 0xd06 | [kunpeng950-microarchitecture.md](kunpeng950-microarchitecture.md) |

## 使用方式

根据目标平台的处理器型号，阅读对应的微架构文档获取以下信息：

- **流水线结构**：取指、译码、发射、执行、写回各阶段宽度和深度
- **缓存层级**：L1I/L1D/L2/L3 容量、延迟、关联度
- **功能单元**：ALU、FPU、NEON/SVE 执行单元数量和延迟
- **分支预测**：预测器类型、BTB 大小、误预测惩罚
- **SIMD 能力**：NEON 128-bit / SVE 向量宽度
- **内存子系统**：Load/Store 队列深度、预取策略

## 指令延迟查询

`scripts/` 目录包含 JSON 格式的指令延迟/吞吐数据库：

- `tsv110_full.json` — Kunpeng-0xd01 (TSV110) 指令延迟数据
- `uarch_b_full.json` — Kunpeng-0xd03 指令延迟数据

```bash
# 查询特定指令延迟
python3 scripts/query_tsv110.py "FADD"
python3 scripts/query_uarch_b.py "FMLA"
```

## 目标平台 ISA 能力

`scripts/isa_capabilities.json` 提供鲲鹏各型号的 SIMD/ISA 能力矩阵（NEON/SVE/SME/DotProd/FP16/BF16/I8MM），供向量化选型使用：

```bash
# 查看目标平台能力（以能力矩阵为准，不依赖本机）
detect_isa_features.sh --target 0xd01 --json
detect_isa_features.sh --target 0xd06 --require sve
```

0xd01 (TSV110) 仅有 NEON，无 SVE/SME；0xd03/0xd06 支持 SVE/SVE2、DotProd、FP16、BF16、I8MM。SME 在三个型号上均不支持。
