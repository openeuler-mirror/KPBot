# 结果目录契约

所有过程文件落在**被优化项目的同级目录**下的 `simd_optimization_result/`。
项目仓库里只留最终验证过的 SIMD 代码，别的什么都不留。

## 位置

```
<parent>/
├── <project>/                       # 被优化的项目，只放验证过的代码
└── simd_optimization_result/        # 本次全部过程与结果
```

同级而不是项目内，是因为结果目录里有编译产物、原始样本、A/B 二进制，放进项目会
污染 `.gitignore`、构建系统和代码搜索。用户看结论时两个目录并排打开也方便。

路径由 `scripts/init_workspace.py` 计算并创建，不要手写：

```bash
python3 <skill>/scripts/init_workspace.py --project <project-root>
```

## 结构

```
simd_optimization_result/
├── simd_optimization_report.md      # 唯一最终产物（顶层只放 md）
├── process/                         # 每个函数的优化过程
│   └── <fn>/
│       ├── record.json              # 结构化记录，最终报告的输入
│       ├── summary.md               # 该函数的优化总结（每函数一份）
│       ├── ref/                      # 优化前的原始实现（基线，权威副本）
│       │   └── <file>.<fn>.ref.c
│       ├── proposed/                 # 未通过验收的候选（unverified 时放这里）
│       ├── diff.patch                # 相对项目的改动
│       └── notes.md                  # 手法选择、失败尝试、判断依据
├── test/
│   └── <fn>/
│       ├── cases.json                # 用例矩阵与期望
│       ├── inputs/                   # 输入样本（大文件可只放生成脚本）
│       ├── outputs_ref/              # 基线输出
│       ├── outputs_simd/             # SIMD 输出
│       └── result.json               # compare_outputs.py 的结论
├── build/
│   ├── <fn>/                         # 该函数的编译产物、编译命令日志
│   └── logs/
├── other/
│   ├── env.json                      # 环境快照
│   ├── hotspots/                     # 热点来源与解析结果
│   │   ├── source.json
│   │   └── raw/                      # perf/gprof 原始输出
│   ├── web-search/                   # 检索记录与链接
│   ├── reference-digest.md           # 读了 summary/ 方法库的哪些文件、用了哪些方法
│   ├── report-notes.md               # 报告里手写的章节（结论/风险/建议）
│   └── state.json                    # 本次调用的三问答案 + 进度（run_started_at 区分新旧调用）
└── .gitignore                        # 结果目录本身不入库（见下）
```

### 为什么这样分

- `process/` 和 `test/` 分开：过程是"我怎么想的"，测试是"凭什么说它对"。评审时
  两者要能对上，但不能混在一起。`record.json` 是两者的连接点。
- `ref/` 放在 `process/` 而不是 `test/`：它是**被测代码的一部分**，测试目录只放输入
  输出。基线版本和候选版本都在 `build/` 里被编译。
- `build/` 单独出来：能被删掉重建的东西不进 `process/`，否则 diff 会被 `.o` 淹没。

## `record.json` 契约

`scripts/render_report.py` 读它生成最终报告，字段名不能改：

```json
{
  "schema": 1,
  "function": "pixel_ssd_4x4",
  "file": "source/common/pixel.cpp",
  "line": 412,
  "started_at": "2026-01-01T10:00:00+08:00",
  "finished_at": "2026-01-01T10:42:00+08:00",
  "status": "verified",
  "verify_method": "native",
  "reference": {
    "summary_files_read": [],
    "matched_methods": [],
    "deviations": "",
    "web_sources": []
  },
  "analysis": {
    "why_hot": "8x8 块的 SSD 在最内层，每次调用 64 个像素",
    "vectorizable": true,
    "hazards": ["16 位中间和溢出", "n 不是 VL 整数倍"],
    "chosen_technique": "扩宽装载 + 点积累加 + 谓词尾部"
  },
  "correctness": {
    "verdict": "pass",
    "cases": 7,
    "mismatches": 0,
    "bit_exact": true,
    "evidence": "test/pixel_ssd_4x4/result.json"
  },
  "performance": {
    "harness": "qemu",
    "harness_detail": "qemu-system-aarch64 -M virt -cpu max -icount shift=0",
    "timer": "cntvct_el0",
    "deterministic": true,
    "rounds": 5,
    "ref_ns": 0,
    "simd_ns": 0,
    "speedup": 0,
    "verdict": "faster",
    "command": ""
  },
  "attempts": [
    {"technique": "谓词单循环", "speedup": 0.98, "result": "not_faster"}
  ],
  "reject_reason": "",
  "notes": ""
}
```

`status` 取值固定为 `verified` / `validated` / `correct_only` / `rejected` /
`unverified`，含义见 `verification-gates.md` 第四节。新增取值只能追加，不能改语义。

`verify_method` 是**用户选的**验证方式，取值固定 `native` / `qemu`；`performance.harness`
用同一套词，具体命令写进 `performance.harness_detail`。两者不一致说明记录写错了。
两种口径的数字不可比，报告里会分开渲染。

`performance.deterministic` 很重要：`-icount` 下为 `true`（数字可复现），墙钟为
`false`（需要带离散度）。报告里会据此调整结论措辞。

## 每个函数的总结

`process/<fn>/summary.md` 是给**这个函数**的一份小结，用户会直接读。模板：

```markdown
## pixel_ssd_4x4（source/common/pixel.cpp:412）

**为什么是热点**：perf 占比 12.3%，8x8 块在最内层，每次 64 个像素。

**问题**：16 位输入逐像素做差后平方，32 位累加；原始实现逐元素循环，
每次只处理 1 个像素，且 `n`（块内像素数）固定为 64。

**手法**：扩宽装载（`svld1uh_s32`）后做 `svdot` 累加，尾部用谓词。
参考了 summary/ 方法库的「用扩宽加载直接得到 16 位残差」，把 `svsub` 换成
`svabd` 因为原实现取的是绝对值。

**正确性**：7 条用例全过，位精确。

**性能**：ref 230471 → simd 128900 ticks，1.79x（qemu icount，动态指令数）。

**残留风险**：只测了块对齐输入；真实编码器里可能有非对齐块。
```

模板的意义是让每个函数都回答同样五个问题。缺"残留风险"的小结在评审时会被打回，
因为读的人无法判断这个改动什么时候会出问题。

## 最终报告

顶层 `simd_optimization_report.md` 是**唯一**的最终产物，只放一份，不要放 HTML、
PDF、JSON。生成方式：

```bash
python3 <skill>/scripts/render_report.py --workspace <simd_optimization_result>
```

脚本负责把 `process/*/record.json` 聚合成表格和统计。判断性的段落写进
`other/report-notes.md`：

```markdown
## 结论
<3~5 句>

## 补充风险
- <本函数特有的风险>

## 下一步建议
1. <按收益/风险排序，最多 3 条>
```

脚本按 `## 标题` 读取并注入，所以重复生成报告不会丢手写内容。直接把结论写在报告里
是行不通的：补一条 record 或改一个数字就要重跑脚本，手写段落会被覆盖。

## 不得写入项目的东西

写回项目时逐个检查，以下都不允许出现在项目目录里：

- `simd_optimization_result/` 的任何子目录；
- 基准夹具（`crt0.S`、`link.ld`、`bench_runner.c` 等）；
- `*_ref.c` 基线副本、`.o`、`.elf`、`gmon.out`、`perf.data`；
- 测试输入样本（YUV、位流、图片）；
- 临时 patch、备份文件（`*.orig`、`*.rej`、`*~`）。

只有"被验证过的 SIMD 实现本身"进项目。用户的仓库应当看起来像"这个人重写了几个
函数"，而不是"有人在这里做过一次实验"。

## 结果目录自身不入库

`init_workspace.py` 会在结果目录里写一个 `.gitignore`（`*`），防止有人误把结果目录
加进某个更大的仓库。如果项目本身是 git 仓库，还要检查 `git status` 确认没有残留：

```bash
git -C <project> status --porcelain
```

输出里只应该出现被修改的源文件。
