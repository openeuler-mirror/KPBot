# 扫描与配对手册

扫描器只负责把可能相关的函数摆到桌面上。真正决定“这是不是一次 C→NEON / C→SVE / NEON→SVE 改写”的，是前后两个函数体的语义和指令。本手册说明扫描器怎么工作、什么时候会漏、以及人工核对时看什么。

## 1. 候选的数据形态

`scan_simd.py` 输出一个 JSON：

```json
{
  "mode": "project",
  "stats": {"files": 0, "functions": 0, "candidates": 0, "by_pair": {}},
  "candidates": [
    {
      "id": "p1",
      "kind": "project",
      "isa_pair": "C_NEON",
      "confidence": 0.95,
      "reason": "dispatch-field",
      "before": {"name": "", "file": "", "line": 0, "end_line": 0,
                 "lang": "c", "isa": "c", "neon_score": 0, "sve_score": 0,
                 "key": ""},
      "after": {"name": "", "file": "", "line": 0, "end_line": 0,
                "lang": "c", "isa": "neon", "neon_score": 0, "sve_score": 0,
                "key": ""}
    }
  ]
}
```

- `isa` 是函数体的主导指令集（`c` / `neon` / `sve` / `mixed`），`neon_score` / `sve_score` 是独立证据计数。分数低但非零，通常说明这个函数只是调用了向量 helper，或者是一个分发包装。
- `confidence` 只是配对启发式的置信度，不是“这是真实改写”的概率。`reason` 为 `base-name` 表示按去后缀的函数名配对，`dispatch-field` 表示按 `p.xxx = fn` 分发表字段配对，`same-name-modified` 表示同一文件同名函数体变化，`vector-impl-added` 表示新仓库新增向量实现后配到参考实现。
- `before` / `after` 的 `file` / `line` 只用于本次扫描定位，**绝不能写进 summary**。

## 2. ISA 识别特征

| 指令集 | 强特征 | 常见类型/寄存器 | 备注 |
| --- | --- | --- | --- |
| C | 没有向量 intrinsic；可能有 `_c` 后缀、模板参数、标量循环 | `int`、`uint16_t`、数组 | 也可能调用 `_neon` helper，此时函数体本身仍是 C 调度 |
| NEON | `#include <arm_neon.h>`；`vld1q_*`、`vst1q_*`、`vaddq_*`、`vmlaq_*`、`vmlal_*`、`vabdq_*`、`vmaxvq_*`、`vaddvq_*`、`vqtbl*`、`vbslq_*`、`vdotq_*` | `uint8x16_t`、`int16x8_t`、`int32x4_t` | AArch64 汇编里是 `v0.16b`、`v1.8h`、`ld1 {v0.16b}` |
| SVE | `#include <arm_sve.h>`；`svld1*`、`svst1*`、`svwhilelt*`、`svptrue*`、`svaddv*`、`svtbl*`、`svsel*`、`svcnt*` | `svbool_t`、`svuint8_t`、`svint32_t` | 汇编里是 `z0.s`、`p0/z`、`ptrue`、`whilelt`、`cntw` |

判断顺序：

1. 先看函数体，不看文件名。`*-sve.cpp` 可能整篇是 NEON intrinsic；`*-neon.S` 里也可能有 SVE 桥接。
2. 同时出现 NEON 和 SVE 时看目标侧：如果新增代码以 SVE 为主、NEON 只用于 128-bit 收尾或桥接，按 SVE 归类；如果只是把 NEON 函数重命名成 `_sve`，不要当成 SVE 改写。
3. 汇编函数按 `function PFX(name)` 到 `endfunc` 之间统计 `z` 寄存器/`ptrue`/`whilelt` 与 `v` 寄存器/`ld1`/`add v` 的出现次数。

## 3. project 模式：同一项目内的多份实现

### 3.1 扫描器用的三条线索

1. **函数名基名**：去掉 `_neon`、`_sve`、`_sve2`、`_neon_dotprod`、`_neon_i8mm`、`_c`、`_WxH`、尾部尺寸数字，再做 `interp4_` → `interp_` 之类的归一化。适合 `pixel_sad_4x4_neon` 与 `pixel_sad_4x4_sve` 这种同族命名。
2. **分发表字段**：在 `setup*Primitives*` 一类的函数里解析 `p.cu[...].sse_pp = PFX(pixel_sse_pp_4x4_sve)`，把左值字段相同的赋值归为一组。适合 C 模板名和 NEON/SVE 实现名完全不同、或尺寸变体很多的情况。C 侧宏（`#define LUMA_PU(...)`）里的赋值也在函数体内，扫描器能读到。
3. **同文件同名**：`different` 模式用来找“同一函数被改写”，`project` 模式也可以用来发现同一文件里的标量/向量重载。

### 3.2 人工核对清单

对每一对 `before` / `after`：

- [ ] 参数个数、类型、步长、系数、边界语义一致；尺寸变体对得上（`4x4` 不要配 `8x8`）。
- [ ] `before` 确实是被替换的标量/旧向量实现，不是另一个尺寸或另一个 bit-depth 的重载。
- [ ] `after` 的函数体确实包含目标 ISA 的运算，不只是调用同名 helper 或只做 `switch` 分发。
- [ ] 差异集中在“怎么向量化”，而不是无关的重命名、格式、日志、构建分支。
- [ ] 能找到至少一个“修改前没有、修改后有”的具体手段（否则可能只是一次重构）。

### 3.3 常见误配

| 现象 | 为什么错 | 处理 |
| --- | --- | --- |
| `foo_neon` ↔ `foo_sve`，但两边都只有 NEON intrinsic | 文件名是 SVE，函数体没换 | 归入 C_NEON 或跳过 |
| `foo` ↔ `foo_neon`，`foo` 只是 `switch` 后调用 `foo_neon` | 这是分发/桥接，不是标量到向量 | 跳过；若桥接本身有复用价值，单独总结“分发桥接”并说明 |
| `foo_4x4` ↔ `foo_8x8` | 尺寸不同，数学对象不同 | 换同尺寸候选，或降级为“同族方法”但不要写成同一函数 |
| `foo_8bit` ↔ `foo_16bit` 重载 | 数据类型不同，指令路径不同 | 分别配对；不要把两个重载混成一个例子 |
| `foo` ↔ `bar`，仅名字相似 | 语义不同 | 读签名和调用点，确认是同一操作才保留 |

## 4. different 模式：前后两个仓库

### 4.1 两类候选

1. **modified**：同一相对路径、同名、同参数个数的函数，函数体变化，且前后 ISA 属于目标方向。适合“原来 C 函数体被改成 NEON”“原来 NEON 实现被改成 SVE”。
2. **added**：新仓库新增的 NEON/SVE 函数，扫描器在旧仓库或新仓库里找同基名或同分发表字段的 C/NEON 参考实现。适合“保留 C 参考实现，新增向量实现”的常见做法。

`different` 模式不要求新旧仓库有 git 关系；按文件树逐函数对比即可。两个仓库的目录布局可以不同，但同名函数要能在相对路径或基名上对上。

输出里的 `unpaired` 列出“新增了向量实现、但扫描器没找到参考实现”的函数。不要把它当成噪音直接丢掉：按签名、调用点、分发表字段或相邻的 NEON 实现手工找一次；确实找不到对应关系的再跳过，并在最终 `warnings` 里说明还有哪些函数族没有覆盖。

### 4.2 人工核对清单

- [ ] 这是“向量化改写”，不是上游同步、bug fix、格式整理、构建改动。
- [ ] `before` 是旧仓库里真实存在的实现；不要把新仓库里的另一个向量实现当成 before。
- [ ] `added` 候选的参考实现选对了：优先同尺寸、同数据类型、同分发表字段；没有就跳过，不要硬配。
- [ ] 如果旧仓库已有 NEON、新仓库新增 SVE，确认新增的是 SVE 代码，而不是把 NEON 文件改名。
- [ ] 如果函数被移动或重命名，用调用点/分发表/signature 找对应关系，不要只靠文件名。

### 4.3 常见误配

- 旧仓库的 C 函数在新仓库里被整体重构，新增的 `_neon` 实现其实对应另一个函数；看调用点和分发表字段。
- 同一个基名有多个 bit-depth / 色度格式重载，`added` 配到了错误的重载；用参数类型和 `#if HIGH_BIT_DEPTH` 区分。
- 新仓库新增的是测试、性能测试或构建脚本里的函数；只总结生产代码路径。
- 只改了 `#include` 或宏，函数体 ISA 没变；跳过。

## 5. 扫描器会漏什么

- 宏生成的函数（`#define DEFINE_FOO(...)` 展开出的定义）通常不会出现在函数清单里。
- 模板实例化后的名字和源码里的模板名不同；按模板名配对，不要按实例化名。
- 函数指针表、运行时分发、`std::function` 注册的函数可能没有 `setup*` 赋值那么直观。
- 内联在头文件里的 helper 可能被算作独立函数，配对时要看它是否只是被调用的工具。
- 汇编里的局部标签、宏参数（`\h`、`PFX`）可能让名字提取不完整；用 `grep` 找真实符号名。

漏了就用兜底检索：

```bash
# SVE 代码
grep -rn "arm_sve.h\|svwhilelt\|svld1\|svptrue" --include='*.c' --include='*.cpp' --include='*.S' <root>

# NEON 代码
grep -rn "arm_neon.h\|vld1q_\|vmlaq_\|vaddvq_" --include='*.c' --include='*.cpp' --include='*.S' <root>

# 命名族
grep -rn "_sve2\?\b\|_neon\b" --include='*.c' --include='*.cpp' --include='*.S' <root>
```

## 6. 分批与恢复

大仓库一次读几百个函数不现实，按下面方式分批：

1. 先按目录或文件族分组（例如滤波、变换、像素统计、熵编码），每组 5～10 对。
2. 每批读完就写 bundle、合并、再读下一批；合并脚本会按 slug 和代码内容去重，重跑不会产生重复。
3. 上下文快满时，先把当前批的方法落盘，再在最终报告里把 `coverage` 标成 `partial`，写清剩余范围。
4. 不要用“候选太多”作为跳过全部函数的理由；至少完成一个完整文件族，并如实报告覆盖范围。

## 7. 什么时候该停下来问用户

- 路径不存在、不是目录，或新旧路径相同。
- 扫描范围内没有任何候选，而用户确信有 SIMD 改动（可能是私有分支、生成代码或非标准后缀）。
- 同一操作有多套候选，语义无法从代码判断（例如两套不同边界策略），且选择会改变总结结论。
- 用户要求把来源、性能数据或性能测试写进 summary；这违反本技能的复用前提，应说明原因并拒绝该部分。
