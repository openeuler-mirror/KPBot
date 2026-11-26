---
name: gemm-abi-std-testing
description: |
  标准符合性测试框架搭建方法论：官方标准条款驱动判定 + 双槽对照（slotA 参照库 / DUT 被测库）+ 确定性 seed 派生 + 多通道判定 + 变异测试度量套件完整性 + 多平台一键 CI 与基线 CSV 回归判定。已在 GEMM/FFT 两类数值计算模块完整实证。
  当用户提到以下场景时触发：
  - 为某模块/库/算子搭建符合性测试、标准符合性测试框架、conformance test
  - 标准获取、标准条文存档、条款数据库、clause 驱动判定
  - 测试框架方案设计（双槽对照、后端抽象、套件自注册、seed 派生）
  - 多平台执行结果对比、基线回归判定
---

# 标准符合性测试框架搭建

## 概述与核心模型

以官方标准文档的逐条条款为判定依据，双槽对照执行（slotA 参照库 + DUT 被测库），确定性数据生成，多通道判定，变异测试度量套件完整性，多平台一键 CI + 基线 CSV 对比。本方法论已在两类模块（GEMM 类矩阵运算、FFT 类变换）跨领域复用成功。

```
S1 标准获取 → S2 框架设计 → S3 用例设计 → S4 判定容差
→ S5 代码编写与自校验 → S6 多平台执行对比 → S7 报告与沉淀
```

## 输入

1. **被测模块**：库名、源码/二进制获取方式、对外接口（函数签名、ABI 约定）。
2. **参照库（slotA）**：同接口的参照实现（领域内开源参照实现或 xx 厂家的 xx 库）。
3. **目标平台**：本机 + 远程测试机的架构、工具链、SSH 通道。
4. **领域标准**：若用户未指明，先按 S1 自行调研确定，不臆测。

## 核心原则（必须严格遵守）

1. **代码即真相**：所有结论来自源码级实测或官方文档原文，禁止记忆转述；版本号一律当日实测核对。
2. **条款驱动**：每条断言绑定一条官方标准条款；标准未定义的行为只做"记录行为"断言（WARN/INFO），严禁虚构正确性要求。
3. **归责到槽**：违规必须报告违反哪份文档哪个章节，且按槽归责（slotA 与 DUT 分开判定），禁止把参照库缺陷算到被测库头上。
4. **可复现性**：用例输入由 case 签名确定性派生（禁止运行期计数器）；环境（线程数/绑核）固定写进脚本。
5. **先疑框架**：框架判定未收敛前，先假设"FAIL 全是框架的锅"逐轮排查。

---

## S1. 标准获取

**目标**：确定判定依据，建立条款数据库与原文存档。

1. 按三层选型表确定判定依据：

   | 层 | 标准 | 判定依据 | 适用问题 |
   |----|------|---------|---------|
   | 1 | 领域官方测试驱动 | 驱动内置自检（如 BLAS 领域 netlib 驱动的 Test Ratio < 16.0） | 对外背书 |
   | 2 | 领域标准报告条文 | 框架 oracle + 条款断言 | 语义符合性定位 |
   | 3 | 上层语言标准 | 子条款断言 + 原生抽象 | 上层接口接入 |

2. 建 `standards/` 目录 verbatim 存档：官方 PDF + OCR 全文（工具 `tesseract-ocr + poppler-utils`）、参考实现源码、标准草案快照。禁止只存链接。
3. 建条款数据库 `core/spec.hpp`：每条 `Clause{id, doc, section, text}`，id 稳定、section 注明章节/页码/行号、text 为官方原文逐字摘录。
4. 建 `STANDARDS.md`：核对日期、版本号（官方发布页/GitHub Releases API 实测）、链接可达性、标准演进脉络。
5. 建 `REPOS.md`：相关仓库地址与镜像、已失效链接清单（避免后人踩坑）。
6. 发现标准自相矛盾时：条款库标注 `DECISION REQUIRED`，测试按双口径记录，不强行判 FAIL；进入 S7.3 决策表跟踪。

**条款数据库示例（内联）**：

```cpp
// core/spec.hpp —— 每条断言绑定一条官方条款
struct Clause {
    const char* id;      // 稳定 ID，如 "STD-GEMM-QR"
    SpecDoc     doc;     // 枚举：报告 / 参考实现源码 / 上层标准草案
    const char* section; // "report, Ch.2 §2.8.8 (p.83)" —— 章节/页码/行号
    const char* text;    // 官方原文逐字摘录（verbatim）
};

// 示例条款：quick return 语义（GEMM 语境）
{ "STD-GEMM-QR", SpecDoc::REPORT,
  "report, Ch.2 \u00a72.8.8 (p.83), Quick return",
  "This routine returns immediately if alpha is equal to zero and "
  "beta is equal to one, or if m or n or k is less than or equal to zero." }
```

**产出验收**：任一 FAIL 能输出 `VIOLATION by <槽>: [文档 章节 页码] "条款原文"`，可直接人工核对。

## S2. 框架设计

**目标**：搭建双槽对照执行框架，新增套件/条款/用例/后端零改主体。

1. 双槽架构：`IBackend` 抽象（`name()` + 核心算子接口），slotA=参照库，DUT=被测库；编译宏选择后端（如 `-DGV_USE_REF` 单后端模式，DUT 槽记 SKIP）。违规按槽归责：`A=FAIL DUT=PASS` 与 `A=PASS DUT=FAIL` 分开报告。
2. 套件自注册：一文件一套件（`cases/sNN_xxx.cpp`），文件末尾一行注册宏（`GV_SUITE("name", fn)`），main 零改动。
3. 确定性 seed 派生：case 签名 → `make_case_id` → `derive_seed` → 输入数据；多个输入缓冲用 seed 与固定常量异或派生。保证双库同输入、新增用例不影响旧基线。
4. `mk()` 工厂函数：按 layout/trans 自动推导合法 ld，**ld 下限 floor 到 1**——防止零维用例意外携带非法 ld=0，把被测语义掩盖在库的保守 early-return 之后。
5. CLI：`--dtype / --suite / --filter / --tol / --list-suites` + 非零退出码（0=无 FAIL）接 CI；`--filter` 支持单 case 独立进程复现。
6. 目录结构：`core/`（types/seed/oracle/checker/runner/report/spec/backend/registry）+ `cases/` + `layerN_*/`（各标准层集成）+ `ci/` + `standards/`。

**关键代码示例（内联）**：

```cpp
// 后端抽象：新增后端零改框架主体
class IBackend {
public:
    virtual const char* name() const = 0;
    virtual void gemm(const CaseSpec& c, T alpha,
                      const T* A, const T* B, T beta, T* C) = 0;
};
// 编译期选择：默认 slotA=参照库/DUT=被测库；-DGV_USE_REF 切单后端模式

// 套件自注册（main 零改动）
GV_SUITE("semantics", suite_semantics);

// 确定性 seed 派生（可复现性根基）
c.id   = make_case_id(c.suite, c);          // 签名: "32x32x32|R|NN|a=1,b=1.5"
c.seed = derive_seed(c.id, c.suite);        // 签名相同 => 输入逐位相同
gen_random(A, c.seed);                      // 缓冲 A
gen_random(B, c.seed ^ 0xABCDEF1234567890ULL);  // 缓冲 B（异或常量派生）

// mk() 工厂：自动推导合法 ld，floor 到 1
inline CaseSpec mk(Layout lo, Trans ta, Trans tb, long m, long n, long k) {
    CaseSpec c;
    c.lda = /* 按 lo/ta 推导 */;  c.ldb = /* 按 lo/tb 推导 */;  c.ldc = /* ... */;
    if (c.lda < 1) c.lda = 1;   // ld=0 是非法输入，会触发库的无关错误路径
    return c;                   // 掩盖真正被测的语义 => 必须 floor 1
}
```

## S3. 用例设计

**目标**：九大维度覆盖 + 标准负向用例锁定。

1. 总原则：**每条测试项都映射到一条标准规则**；先查标准条文再写用例，case 上标记 `c.clause = "条款id"`。
2. 九大套件体系裁剪适配：

   | 套件 | 内容要点 |
   |------|---------|
   | s01 semantics | quick return、α×β 全组合（25 组）、零维 m/n/k=0 |
   | s02 precision | subnormal / overflow / large-k / tiny / 负零 / k=2 |
   | s03 edge | 退化形状 / 瘦长 / tile / 路径切换 / 对齐扫描 / ld-padding |
   | s04 errors | 非法参数探测（Linux fork 隔离，崩溃不连坐） |
   | s05 sentry | NaN/Inf 哨兵输入、零矩阵（α=0 不许 NaN 泄漏） |
   | s06 lowprec | bf16/f16/i8，含溢出/下溢边界；i8 期望 bit 级相等 |
   | s07 complex | 复数变体（共轭语义、H 变体） |
   | s08 perf | GFLOPS（含 warmup）+ mtscan 多线程 hash 扫描 |
   | s09 determinism | 同输入多次调用 bit 级一致性 |

3. 边界维度集 `D = {0,1,2,3,5,15,16,17,31,32,33,63,64,65,127,128,129,512,513,1024}` 组合裁剪；对齐敏感性扫描（`n = 240+i, i=0..15`）；实现特有路径切换点 ±1（如 `k ∈ {511,512,513,514}` 验证 small_k/large_k 分界两路径容差内等价）；退化长条形状（`1×1×1024` 等）。
4. 优先级：P0（正确性高危：α/β 边界、类型约束、哨兵保护）> P1（维度/布局/转置组合）> P2（MT/精度/性能）。
5. 负向用例锁定：k=0 且 β≠0（标准最易被误解点，early-return 条款不适用）、β=0（C 输入不得被读）、α=0 + NaN 哨兵 A/B（结果不得依赖 A/B 内容）。

**用例声明示例（内联）**：

```cpp
// cases/s01_semantics.cpp —— 声明式用例
CaseSpec c = mk(Layout::RowMajor, Trans::NoTrans, Trans::NoTrans, 16, 16, 0);
c.alpha = 1.0; c.beta = 1.5;          // k=0 且 β≠0：标准最易被误解的负向点
c.clause = "STD-GEMM-K0";             // 绑定条款：C := β·C（空和）
emit(rn, exec_case<double>(rn, c, "semantics", slotA, dut));
```

## S4. 判定与容差设计

**目标**：判定可解释（级别+条款）、容差不误判、失败可复现。

1. 判定五级：PASS（符合条款，输出 `conforms: [原文]`）/ FAIL（违反条款）/ WARN（标准未定义或未适配口径）/ SKIP（能力缺口，标注原因）/ INFO（行为记录）。
2. oracle 四形态按对象选用：高精度朴素参考（f64 三重循环）/ 回代残差判据（病态敏感运算，比残差不比解）/ 数学性质自检（roundtrip 如 fwd+bwd=n·x、δ 脉冲→已知谱，无需 oracle）/ 官方套件判据。
3. **容差必须 scale-aware**（本方法论最重要的判据原则）：
   - 公式：`8·ε·(k·|α|·max|A|·max|B| + |β|·max|C|)`——**k 放大因子只作用于累加项**，β·C 项不得乘 k；
   - 三模式：`table`（经验表）/ `norm`（范数缩放）/ `bitwise`（双槽逐位一致，仅双后端模式有意义）；
   - 低精度：f16/bf16 用 fp64 oracle + 按输入精度放宽；i8 期望 bit 级相等（整数运算无舍入自由度）；
   - 禁止固定相对误差（如 1e-6 不含 k 缩放）——大 k 必然误判，小 k 漏检。
4. 多通道判定（一个用例同时校验多维语义）：

   | 通道 | 校验内容 |
   |------|---------|
   | Precision | 误差 vs oracle 超容差 |
   | NanSemantics | 非预期 NaN 输出；NaN 源输入允许传播；双槽 NaN 模式一致性只记录不罚 |
   | Sentry | 输入 A/B 调用前后 FNV hash 不变（库不得改写输入） |
   | Bitwise | 双槽输出逐位一致率（信息性/模式判据） |

5. FAIL 自动 dump 六件套：`A / B / C_in / 双槽输出 / f64 参考` + `_meta.txt`（case_id/seed/label/tol_mode）→ `--filter <子串>` 单独进程复现。

**判定输出示例（控制台，内联）**：

```text
  semantics  32x32x32|R|NN|a=0,b=1   A=FAIL DUT=PASS bit=96.88%  {NanSemantics:pattern-mismatch}
     VIOLATION by slotA(xx厂家xx库): [BLAST-2002 report, Ch.2 §2.8.8 (p.83), Quick return]
     "This routine returns immediately if alpha is equal to zero ..."
  semantics  16x16x0|R|NN|a=1,b=1.5 A=PASS DUT=PASS bit=100.00%
     conforms: [BLAST-2002 report, Ch.2 §2.8.8 (p.83)] the summation over k is empty ...
SUMMARY: PASS=746 FAIL=0 WARN=0 SKIP=663
```

## S5. 代码编写与自校验

**目标**：框架自身正确性收敛，套件完整性定量可证。

1. 框架调试收敛流程：每轮 FAIL 先归因到框架 bug，修复后重跑，直到收敛；收敛后剩余 FAIL 才允许归责被测对象。实证收敛路径（5 轮）：ld 未按 trans 取维度 → NaN 判定与毒化位置不一致 → 参考实现缓冲区计数公式写反（越界读堆垃圾，此前"被测库崩溃"全是假阳性）→ mtscan 构造 ld 未按 trans 修正 + 容差公式 k=0 时 β·C 项被 k 因子归零。
2. 三条铁律（假阳性预防）：
   - **"两库结果一致地错" = oracle bug 强特征**（实证两次由此定位框架缺陷）；
   - **容差公式逐项对应数值来源**（k 放大只作用于累加项）；
   - **语义用例不得携带附带非法参数**（实证：ld=0 假阳性曾导致 k=0 错误结论并发布，后正式撤销——报告必须保留撤销记录与根因）。
3. 变异测试（套件完整性度量，强推荐）：对被测路径注入 6 类缺陷，宏开关实现，度量杀灭率（实证 6/6）；正确幸存的变异用例（如 α=0 时 k 截断无影响）本身也是语义佐证。
4. 输入完整性：全部用例对输入做 FNV hash 前后校验，运行级确认无一 MUTATED。
5. `TEST_CASES.md` 由最新一轮运行自动生成（用例/条款/双槽判定逐行列出），用例文档与代码永不漂移。

**变异宏示例（内联）**：

```cpp
// 编译期注入缺陷，验证套件能否杀灭（-DGV_MUTANT_BETA 等）
void gemm(const CaseSpec& c, float alpha, const float* A,
          const float* B, float beta, float* C) override {
    CaseSpec m = c;
#ifdef GV_MUTANT_TRANS
    m.transA = (m.transA == Trans::Trans) ? Trans::NoTrans : Trans::Trans; // 转置翻转
#endif
#ifdef GV_MUTANT_BETA
    float b2 = 0.0f;   // β 置 0（忽略 β）—— 实测 115/115 β 敏感判定 FAIL
#else
    float b2 = beta;
#endif
#ifdef GV_MUTANT_K
    if (m.k > 0) m.k -= 1;  // 截断累加 —— 实测 248 判定 FAIL
#endif
    dut_gemm(A, m.lda, B, m.ldb, C, m.ldc, m.m, m.n, m.k, /*…*/ alpha, b2);
}
// 六类：TRANS/BETA/ALPHA/LDA/LAYOUT/K；杀灭率 = 套件完整性定量证据
```

## S6. 多平台执行与结果对比

**目标**：构建矩阵可重复、一键 CI、基线回归可判定。

1. 构建矩阵 + `requirements.txt`：pip 部分（注明内网镜像）+ 系统级部分分平台注释（工具链版本、静态库、运行时 dll）。
2. 混合工具链链接技巧：ABI adapter 目标文件放在参照库归档**之前**（符号赢者通吃）；Fortran ABI 细节（尾下划线、引用传参、隐藏 char 长度）在 adapter 内吸收。
3. 环境固定：线程数与 NUMA 绑核写进 CI 脚本与所有运行命令（如 `OMP_NUM_THREADS=38 numactl -C 38-75`）。
4. 层 1 官方驱动集成模板：复制驱动+参考输出数据 → 混合编译 → 运行（驱动非零退出不中断）→ `tail SUMM` + `grep "END OF TESTS"` + `grep -ci "FAIL|FATAL|INCORRECT"` 计数。
5. 一键 CI：`ci/run_conformance.sh all|1|2|3`，DUT FAIL 计数 → 非零退出码；组件缺失 SKIP 不误报。脚本须以自身目录为基准（入口处 `cd "$(dirname "$0")"`）或全程使用框架根相对路径，保证从任意 cwd 调用都能正常运行。
6. 基线对比四规则：同参数重跑（seed 派生保证旧用例数据不变）→ CSV 按 `(suite,shape,layout,trans,α,β)` 连接比较 verdict/maxerr/bit_equal_pct → **新增 FAIL=回归；FAIL→PASS=修复；WARN 变化=口径适配进展** → bit 列仅双后端模式有意义。

**CI 入口示例（内联，可直接套改）**：

```bash
#!/bin/bash
# ci/run_conformance.sh —— 一键三层，任一 DUT FAIL => 非零退出码
set -e
ENV="env OMP_NUM_THREADS=38 numactl -C 38-75"   # 环境固定，可复现
RC=0
bash layer1_netlib/run_drivers.sh || RC=1        # 层1 官方驱动自检
$ENV ./verify --dtype all --tol norm --out ci_l2.csv > ci_l2.log 2>&1 || RC=1
F=$(grep -c "DUT=FAIL" ci_l2.log || true)        # 按槽归责：只数 DUT 槽
[ "$F" = "0" ] || { echo "LAYER2: $F DUT failures"; RC=1; }
if [ -x layer3_cxx/test_upper ]; then
    ./layer3_cxx/test_upper || { echo "LAYER3: 上层标准测试 FAILED"; RC=1; }
else
    echo "SKIPPED: 上层标准二进制不在本机构建"
fi
echo "CONFORMANCE RC=$RC"; exit $RC
```

**实测有效性背书（方法论产出的成绩单形态）**：被测加速库三层全绿（官方驱动 17496×2 PASSED / 条款判定 0 FAIL / 上层标准 14/14 COMPLIANT）；参照库某版本暴露 α=0 NaN 泄漏，升级后修复（测试反馈进入上游）；6/6 注入缺陷全部捕获。

## S7. 报告与知识沉淀

**目标**：换人可跑、错误可预防、口径可追溯。

1. 报告四件套：
   - `TEST_REPORT.md`：轮次级报告，**必须保留撤销记录**（错误结论正式撤回并写明根因）；
   - `*_STANDARD_REPORT.md`：用例级逐行明细（shape/dtype/α/β/期望/实际/最大误差/条款）；
   - `TEST_CASES.md`：运行自动生成，与源码一一对应；
   - `results_*.csv` + `mut_*.csv`：机器可读基线与变异证据，支撑对比。
2. 错误收集与预防手册：错误签名跨全部产物（CSV/log/报告）累计计数降序；每条五要素（现象签名/次数/根因/标准冲突条款/来源文件:行号或上游 issue 链接）；本地实测 + 上游 issue（GitHub API 实时检索）+ 标准条款三类数据源交叉；变异预期失败单列不计入真实错误。
3. 条款口径决策记录表：条款 / 采用口径 / 状态（⏳待适配、⚠️DECISION REQUIRED、✅已定）；被测库适配后 WARN→PASS 自动兑现。**决策表示例（内联）**：

   | 条款 | 口径 | 状态 |
   |------|------|------|
   | 错误处理（错误码+回调） | 双口径记录 | ⏳ 待被测库适配 |
   | k=0/β≠1（标准自相矛盾） | 双口径 + DECISION REQUIRED | ⚠️ |
   | 上层标准复数共轭 | 遵循草案 R5 | ✅ |

4. 用户指南：标准选择决策表（"要回答什么问题→用哪层→什么命令"）、运行命令、结果解读、基线对比、条款/用例/套件/后端四条扩展路径。

---

## 落地检查清单（逐阶段打勾）

**阶段一：标准获取**
- [ ] 三层判定依据确定并写入架构表
- [ ] `standards/` verbatim 存档（PDF+OCR / 源码 / 草案快照）
- [ ] `STANDARDS.md` 版本当日成立 + 链接可达性
- [ ] `core/spec.hpp` Clause{id,doc,section,text} 条款数据库
- [ ] 标准自相矛盾点 → 双口径 + DECISION REQUIRED

**阶段二：框架方案**
- [ ] 双槽 IBackend 抽象，编译宏选择
- [ ] GV_SUITE 套件自注册，一文件一套件
- [ ] seed 派生（case 签名 → 稳定 seed → 输入），禁止运行期计数器
- [ ] CLI（--suite/--filter/--tol/--dtype）+ 非零退出码
- [ ] mk() 工厂 ld floor 1

**阶段三：用例与判定**
- [ ] 九大套件裁剪适配
- [ ] 边界维度集 + 对齐扫描 + 路径切换点 ±1 + 退化形状
- [ ] scale-aware 容差（k 因子只乘累加项）+ 三 tol 模式
- [ ] 多通道判定（Precision/NanSemantics/Sentry/Bitwise）
- [ ] FAIL 六件套 dump + --filter 复现

**阶段四：自校验**
- [ ] 框架调试收敛（先假设 FAIL 全是框架 bug）
- [ ] 三铁律检查
- [ ] 变异测试 6 类缺陷注入（trans/beta/alpha/lda/layout/k），度量杀灭率
- [ ] 输入 FNV 校验 + TEST_CASES.md 自动生成

**阶段五：多平台执行**
- [ ] requirements.txt（pip 镜像 + 系统级分平台注释）
- [ ] 线程/绑核固定写进脚本；混合工具链链接顺序验证
- [ ] ci/run_conformance.sh 一键三层（缺失组件 SKIP 不误报）
- [ ] CSV 基线对比四规则
- [ ] 报告四件套 + 错误收集手册 + 口径决策表

## 执行纪律

- 阶段串行执行，前序产出验收未过不进入下一阶段。
- 探针先行：库行为存疑时先写 `probe_*.cpp` 独立探针拿实测证据，再写条款断言（实证：某商用库 2D packed 输出布局堆损坏由此发现——探针实锤后才能写判定）。
- 结论随证据更新：错误结论必须正式撤销并在报告中保留撤销记录与根因。
