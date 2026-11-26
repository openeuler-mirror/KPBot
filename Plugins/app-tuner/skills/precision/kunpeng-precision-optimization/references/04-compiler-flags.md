# 编译器与数学库：选项层调优

## 三档选项策略（F-1 ~ F-3）

按验收等级选档：L2（结果一致）走精度优先档；L3（统计等价）可用折中档换性能。

| 档位 | x86 侧参考 | ARM/毕昇侧参考 | 预期 |
|---|---|---|---|
| 性能优先 | `-O3` | `-O3 -ffp-model=fast` | 结果必然不一致，只用于不要求跨平台一致的场景 |
| **精度优先** | `-O3 -fp-model=precise -no-ftz -init=zero -init=arrays` | `-O3 -faarch64-pow-alt-precision=21 -enable-alt-precision-math-functions km_l9 -Hx,124,0xc00000 -ffp-contract=off -finit-zero -mllvm -disable-sincos-opt -MflushZ` | 对齐对侧结果 |
| 折中 | 去掉 `-no-ftz`、`-init=zero`、`-init=arrays` | 去掉 `-ffp-contract=off`、`-finit-zero`、`-disable-sincos-opt` | 部分对齐，靠四指标验收 |

- F-4 调优顺序：先两侧都用 `-O1` + 同一开源数学库（如 SLEEF）验证「精度原本是否就一致」，再按档位升级选项。
- 档位不是永久配置：每次升级编译器版本都要重验（选项语义随版本变）。

## 一键对齐开关（F-5 / F-6）

`-ffp-compatibility=<代数>` 把一整套「与非 ARM 平台对齐」的小选项打包，常用 18 / 21
（数字对应目标侧编译器代际，贴哪个版本就选哪个）：

```
-ffp-compatibility=21 -fp-model=precise
  ≡ -ffp-contract=off -faarch64-pow-alt-precision=21 -Hx,124,0xc00000 \
    -mllvm -enable-alt-precision-math-functions

-ffp-compatibility=18 -fp-model=precise
  ≡ -ffp-contract=off -faarch64-pow-alt-precision=21 -Hx,124,0x800000 \
    -mllvm -enable-alt-precision-math-functions -enable-18-math-compatibility
```

用法：先用一键开关拿「大面一致」，再用单项开关补零星差异；反之（逐项调再收尾）成本高得多。

## 单项开关速查（F-7）

| 现象 | 开关 | 默认 | 作用 |
|---|---|---|---|
| sin/cos 被合并计算导致差异 | `-mllvm -disable-sincos-opt` | 合并 | 禁用 sincos 合并 |
| min/max 语义不同 | `-faarch64-minmax-alt-precision` | — | 对齐非 ARM 结果 |
| 倒数/开方指令精度 | `-mllvm -aarch64-recip-alt-precision` / `-aarch64-rsqrt-alt-precision` | — | 软浮点补偿 |
| 除法被换成乘法 | `-freciprocal-math` | 关 | 允许 `a/b → a*(1/b)` |
| 舍入模式 | `-fno-rounding-math` / `-frounding-math` | 就近舍入 | 是否按 IEEE754 |
| 数学函数内联 | `-frelaxed-math` | 关 | 用内联数学函数 |
| 假定无 inf/NaN | `-ffinite-math-only` | 关 | 影响 NaN/Inf 分支 |
| ±0 差异 | `-fno-signed-zeros` | — | 忽略符号零 |
| 浮点默认宽度 | `-fdefault-double-8` | 单精度 | 改为双精度 |
| 归约顺序重排 | `-mllvm -instcombine-reorder-sum-of-reduce-add` | true | 影响归约末位 |
| 未初始化内存 | `-finit-zero` / `-finit-array`（x86: `-init=zero -init=arrays`） | 无 | 消除伪差异 |
| 非规格化数洗零 | `-MflushZ`（x86: `-no-ftz` 的反面语义见下） | 平台定 | 与 FTZ/DAZ 语义直接相关 |

注：`-MflushZ` 开与 `-no-ftz` 是目标相反的两种表述，配错方向会让极小值区差异变大；改这项必须做极小值用例回归。

## 已知编译器缺陷（F-8）

ICC 在 `-fp-model precise -O3` 下对**循环内顺序求和**做重结合，与 `precise` 语义冲突。

- 证据形态：六个数顺序相加，正确值 `0b01000110010110001100000000110100`；
  ICC `-O3` 得 `...110011`，`-O0/-O1` 得 `...110100`；毕昇 `-O3`/`-O0` 均为正确值。
- 受影响位置（WRF）：
  - `physics/module_cu_bmj.F`（`cu_physics=2`）
  - `physics/module_cu_gf_deep.F`（`cu_physics=3`）
  - `physics/module_ra_goddard.F`（`ra_lw_physics=5` / `ra_sw_physics=5`）
- 处置：只对受影响文件降 `-O1`，其余文件保持 `-O3`（全局降级会白送性能）。

## 数学库层（F-9 ~ F-12）

- 优先用按对侧精度做过兼容的厂商库（如鲲鹏 KML）：多数函数可对齐，未覆盖的接口需向库方提需求。
- 复平方根（`csqrtf`/`zsqrtf`）一类：用 `-faarch64-pow-alt-precision=18/21` 对齐，默认走 `libm`。
- 幂函数：对侧可能走 80 位实现（见差异分类章 D-2）；ARM 无对应单元，只能多精度库或改算法。
- 判定优先级：换库 > 选项补偿 > 改代码。

## 反模式

- 直接把对侧「一整套选项」原样搬过来：某些选项在 ARM 上语义不同或根本不存在。
- 只改全局 `-O` 级别而不看具体文件：一个缺陷文件拖全工程降级。
- 不开 `-finit-*` 就下结论：未初始化内存造成的伪差异会让人追错方向。
