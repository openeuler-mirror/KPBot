# Arm 官方资料索引与刷新策略

检索基线：`2026-04-23`

本文件现在既回答“该看哪份官方资料”，也明确回答“哪些资料能自动抓、哪些只能保留引用链接”。

## 1. 当前固定来源

### 自动抓取来源

- [Arm C Language Extensions (ACLE)](https://arm-software.github.io/acle/main/acle.html)
- [Arm Neon Intrinsics Reference](https://arm-software.github.io/acle/neon_intrinsics/advsimd.html)

当前自动化会从这些页面中提取：

- `NEON` 常用 concrete intrinsics
- `SVE` instruction -> intrinsic family mapping
- `SME / SME2` 分组标题、streaming / ZA 属性与常用 ZA intrinsic 入口
- SME inline asm 指令名的稳定存在性，例如 `SMSTART`、`SMSTOP`、`ZERO`、`FMOPA`、`ST1W`、`MOVA`、`WHILELO`、`PTRUE`、`LD1W`、`FADD`、`FMLA`

### 可选抓取来源

- [Part 1: Arm Scalable Matrix Extension (SME) Introduction](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction)
- [Part 2: Arm Scalable Matrix Extension (SME) Instructions](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction-p2)

这些页面是 Arm 官方 SME 介绍和 SME Instructions 文章，用于核对 `SMSTART` / `SMSTOP`、outer-product、`FMOPA`、tile load/store/move 和 `ZERO {ZA}` 的语法语义。当前脚本会尝试抓取；如果 `developer.arm.com` 返回 `403`，不会失败，而是依赖 ACLE 的稳定抓取结果并把这些 URL 作为 reference-only 来源保留。

### 仅保留引用的来源

- [Arm SIMD: Optimize, Migrate, and Accelerate C/C++ for Peak Performance](https://developer.arm.com/servers-and-cloud-computing/arm-simd)
- [Scalable Matrix Extension: Expanding the Arm Intrinsics Search Engine](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/scalable-matrix-extension-expanding-the-arm-intrinsics-search-engine)
- [DDI0602 SME Instructions](https://developer.arm.com/documentation/ddi0602/latest/SME-Instructions)
- [DDI0602 SVE Instructions](https://developer.arm.com/documentation/ddi0602/latest/SVE-Instructions)

这些页面当前在本环境下直接抓取返回 `403`，所以：

- 保留 URL 和标题进入 `index.json`
- 在 `index.json` 中标记为 `reference-only`
- 不作为 `refresh_arm_intrinsics_db.py` 的解析输入

## 2. 该先看哪份资料

| 你要回答的问题 | 先看哪份来源 |
| --- | --- |
| `#include` 哪个头文件、feature macro 怎么判断 | `ACLE` |
| 这个 `NEON` intrinsic 的具体签名、指令映射是什么 | `Neon Intrinsics Reference` |
| `SVE` 的指令家族应该映射到哪个 intrinsic family | `ACLE` 的 `Mapping of SVE instructions to intrinsics` |
| `SME` 的 streaming、ZA ownership、ZA instruction group 该怎么查 | `ACLE` 的 `SME language extensions and intrinsics` |
| `SME ZA` standalone inline asm 该怎么写 | `docs/sme-za-inline-asm-guide.md` + `query_arm_intrinsics.py search --group inline-asm --isa sme --json` |
| `FMOPA`、`MOVA`、`ZERO {ZA}` 的官方文章说明在哪里 | Arm SME Instructions blog；DDI0602 URL 作为 authoritative reference-only |
| `inline_asm` / `assembly` 的目标语言如何自动选择 | `docs/codegen-style-guide.md` |
| standalone `.S` 的调用约定和寄存器保存怎么判断 | `AAPCS64` |
| 这个 skill 为什么只收录“常用项”而不是全量指令百科 | `README.md` + `references/arm_intrinsics_db/index.json` |

## 3. 仓库内的落点

官方资料不会直接变成散文文档，而是固定落到：

- `references/arm_intrinsics_db/schema.json`
- `references/arm_intrinsics_db/index.json`
- `references/arm_intrinsics_db/neon.json`
- `references/arm_intrinsics_db/sve.json`
- `references/arm_intrinsics_db/sme.json`
- `references/arm_intrinsics_db/attributes.json`

然后再生成：

- `docs/arm-intrinsics-manual/README.md`
- `docs/arm-intrinsics-manual/neon.md`
- `docs/arm-intrinsics-manual/sve.md`
- `docs/arm-intrinsics-manual/sme.md`
- `docs/arm-intrinsics-manual/correctness-rules.md`

## 4. 刷新流程

### 4.1 刷新 JSON 快照

```bash
python3 scripts/refresh_arm_intrinsics_db.py
```

职责：

- 抓取 `ACLE` 和 `Neon Intrinsics Reference`
- 可选抓取 Arm 官方 SME Introduction / SME Instructions blog；`403` 时不失败
- 校验 curated whitelist 是否还能在官方页面中找到
- 刷新 JSON 快照

### 4.2 重建多级手册

```bash
python3 scripts/generate_arm_intrinsics_manual.py
```

职责：

- 从 JSON 快照生成 Markdown 手册
- 不再手写 per-intrinsic 文档

### 4.3 查询和规则校验

```bash
python3 scripts/query_arm_intrinsics.py lookup --name svwhilelt_b32
python3 scripts/query_arm_intrinsics.py validate-snippet --file ./candidate.c --isa sme --json
```

## 5. 当前收录边界

当前知识库刻意只收录：

- `NEON`、`SVE`、`SME`
- 与向量化直接相关的常用 `SVE2`、`SME2`
- 当前 skill 需要的 attribute、ownership 和静态规则

不收录：

- 全量 Arm 指令百科
- 与当前 skill 无关的完整体系结构扩展树
- 需要依赖 `developer.arm.com` 搜索引擎在线查询才能稳定定位的全量数据
