# summary 模式：SIMD 向量化方法总结

本文件是 summary 模式的完整指令。你把代码里的向量化改写提炼成可跨项目复用的“场景 + 手法 + 代码切片”，写入本技能的 `summary/`。你只做总结：不修改被扫描的代码，不评价性能，不记录任何来源信息。目标读者是下一次要在另一个项目里做同类改写的人或 Agent，因此方法必须脱离原项目仍然可读、可用。

入口的[模式选择](../../SKILL.md)已经完成；确认用户选择的是 summary 后再执行本文件。文中的 `<skill>` 指技能根目录，所有相对路径都按技能根目录解析；方法库固定为 `<skill>/summary/`，由本模式维护、由 [optimize 模式](optimize.md) 复用。

## 输入

用户可以在对话中直接给出，也可以以 JSON 形式给出：

```json
{
  "mode": "project",
  "project_path": "<project-root>",
  "old_path": "",
  "new_path": "",
  "scope": ["src"],
  "merge": true
}
```

- `mode`：summary 模式的子模式，取 `project` 或 `different`。`project` 扫描一个项目内同一操作的 C / NEON / SVE 多份实现；`different` 对比修改前仓库与修改后仓库。
- `project_path`：`project` 子模式必填，指向项目根目录。
- `old_path` / `new_path`：`different` 子模式必填，分别指向修改前、修改后的仓库根目录。
- `scope`：可选，限定相对路径或目录（如 `src`），用于排除构建产物、测试和无关子项目。缺省扫描全部源码。
- `merge`：可选，默认 `true`。`false` 只做本次分析、不写入 `summary/`，用于预览。

**不要猜子模式或路径。** 如果用户没有明确给出，用 `ask_user_question` 询问；两个问题可以分开问，但不要用默认值替用户决定。入口处已经确认过 summary，这里不再重复问主模式。

## 执行步骤

### 步骤 1：确认子模式与路径

1. 先问子模式，选项描述要说明差异：
   - `project`：一个项目里同时存在同一函数的 C / NEON / SVE 实现，总结它们之间的转换。
   - `different`：给出修改前和修改后两个仓库，总结前后函数的向量化转换。
2. 再问路径（`project` 一个，`different` 两个）。用户已经给出时跳过对应提问。
3. 校验：路径存在且是目录；`different` 的旧、新路径不能相同；不要把技能自己的 `summary/` 当作扫描目标。校验失败就停下来问，不要降级成猜路径。

**每次调用都重新确认子模式和路径，不沿用上一次调用的选择。** 把确认结果记下来，最终报告要用。

### 步骤 2：准备 summary 目录

运行：

```bash
python3 <skill>/scripts/init_summary.py --skill-root <skill>
```

它只做三件事：缺 `summary/manual/` 时创建、缺各方向的 `summary.md` 时创建、缺 `README.md` 时创建。已经存在的 `summary.md` 和 `README.md` 不会被覆盖；`summary/manual/` 一旦存在就归用户所有，不要写入、不要“整理”、不要删除。

然后读一遍 `references/modes/summary/output-contract.md`，确认本次要产出的 bundle JSON 和文件格式。不要凭记忆写字段。

### 步骤 3：扫描候选

按子模式运行扫描器：

```bash
# project
python3 <skill>/scripts/scan_simd.py project \
  --root <project-root> --out /tmp/simd-scan.json

# different
python3 <skill>/scripts/scan_simd.py different \
  --old <old-root> --new <new-root> --out /tmp/simd-scan.json
```

大仓库先用 `--include 'src'` 或 `--exclude 'test|build'` 缩范围；`--max-candidates` 只用于分批，不要用它偷偷漏掉函数。

扫描器输出的是**候选**，不是结论：

- `project`：按函数名基名和分发表字段（如 `p.cu[...].sad = ...`）把同一操作的多份实现分组，给出 `isa_pair`、`confidence`、`reason`。
- `different`：同一文件同名函数体变化的 `modified` 候选，以及新仓库新增向量实现并配对到 C/NEON 参考实现的 `added` 候选。
- `stats` 里的 `by_pair` 是候选数量，不是方法数量。

扫描器不解析宏展开、不展开模板、不做语义等价判断；漏掉的函数用 `references/modes/summary/scan-playbook.md` 里的 grep 兜底。**候选为空时不要编造总结**：报告没有找到可总结的转换，并说明扫描范围和过滤条件。

### 步骤 4：逐函数核对并提炼方法

核心要求是**一个函数一对地读**，不是看文件级 diff 猜。对每个候选（建议每批 5～10 对）：

1. 读修改前和修改后的完整函数体，而不是只看 hunk。确认它们做同一件事：输入输出语义、步长、边界、系数、数据类型一致。
2. 确认这是目标方向的 SIMD 改写：修改前是标量 C，修改后使用 NEON/SVE；或修改前 NEON，修改后 SVE。按**代码实际使用的指令**归类，不要只看文件名或函数后缀。例如 `_sve.cpp` 里如果只有 NEON intrinsic，应归入 `C_NEON`，除非它确实使用了 SVE intrinsic 或 SVE 汇编。
3. 排除以下候选：纯分发/包装函数、只改格式或修 bug、只改构建、只加 include、两边的块尺寸或重载不匹配、参考实现选错。
4. 提炼“方法”：是什么变换让向量化成立（循环切分、谓词化、数据重排、扩宽/窄化、归约、查表、滑窗复用、指令选择、尾部处理、布局转换等）。方法名要描述**手段 + 解决的问题**，不要带项目名、函数名或指令流水号。
5. 截取最小可读代码：修改前、修改后各 10～40 行，保留类型和 intrinsic，删掉与本方法无关的循环外壳、日志、断言。多个函数用了同一方法时，每个函数各保留一个例子，放在同一个 pattern 文件里。
6. 去掉来源：不要写仓库名、库名、文件路径、行号、commit、作者、URL。代码里如果出现项目专有类型或宏，尽量换成通用写法，或在示例说明里写“类型别名按项目定义”，不要把路径留在注释里。
7. 与已有总结比对：打开对应方向的 `summary.md` 和 `patterns/`，同一机制、同一类问题合并到已有 slug；机制或适用条件不同才新建。合并规则见 `references/modes/summary/method-taxonomy.md`。

**分批落盘**：每批分析完立即写 bundle 并合并（步骤 5），不要等全仓库读完再写。这样上下文被截断或中途停止时，已确认的方法不会丢，重跑时靠去重不会重复。

### 步骤 5：合并进 summary

把本批方法写成 bundle JSON（格式见 `references/modes/summary/output-contract.md`），然后：

```bash
python3 <skill>/scripts/merge_summary.py --bundle /tmp/simd-bundle.json --skill-root <skill>
```

合并脚本负责：

- 新方法追加到 `summary/auto/<MODE>/summary.md`，并生成 `patterns/<slug>.md`；
- 已有方法只追加新的场景、要点、代码示例，按内容去重；
- 重建方法目录和 `summary/README.md`；
- 不删除任何已有内容，不触碰 `summary/manual/`。

不要把扫描器输出、候选列表、路径、行号写进 `summary/`；`/tmp` 下的扫描结果只是工作文件。

### 步骤 6：质量门与报告

运行：

```bash
python3 <skill>/scripts/validate_summary.py --skill-root <skill>
```

它检查目录结构、`summary.md` 到 pattern 的链接、`# 修改要点` / `# 修改代码示例` 两段、来源信息和性能内容。有 error 必须修完再重跑；warning（例如某个方向还没有方法）可以保留，但要在最终报告里说明。

最后输出 `simd_summary_result`，并把本次实际写入的文件路径列出来。

## 输出

完成后输出一个 JSON 块（不要输出其他顶层字段）：

```json
{
  "simd_summary_result": {
    "success": true,
    "mode": "project",
    "paths": {
      "project": "<project-root>",
      "old": "",
      "new": ""
    },
    "scanned": {
      "files": 0,
      "functions": 0,
      "candidates": 0,
      "verified_pairs": 0
    },
    "by_pair": {
      "C_NEON": 0,
      "C_SVE": 0,
      "NEON_SVE": 0
    },
    "methods": {
      "added": 0,
      "merged": 0,
      "examples_added": 0
    },
    "summary_paths": [
      "summary/auto/C_NEON/summary.md"
    ],
    "coverage": "complete",
    "warnings": [],
    "error_message": ""
  }
}
```

字段说明：

- `paths`：`project` 子模式只填 `project`；`different` 子模式填 `old` / `new`。
- `scanned`：扫描器统计加人工核对结果；`verified_pairs` 是真正读过的函数对数，不是候选数。
- `by_pair`：本次**确认**的三类转换各多少对，未确认的候选不计入。
- `methods`：取合并脚本返回的 `methods_added`、`methods_merged`、`examples_added`。
- `coverage`：允许值固定为 `complete` / `partial`。`complete` 表示扫描范围内所有候选都已核对；因上下文或时间分批而只做了一部分时必须是 `partial`，并在 `warnings` 里写清还剩哪些文件或目录。
- 失败时 `success=false`，`error_message` 写具体原因（路径不存在、没有候选、bundle 校验失败、validate 未通过等），其余数组为空，`coverage` 为 `partial`。

## 规则

- **只总结，不改目标代码。** 唯一允许写入的位置是 `<skill>/summary/`；扫描结果放 `/tmp`。不要修改、格式化或“顺手修复”被扫描仓库。
- **不记录来源。** 仓库名、库名、文件路径、行号、commit、作者、URL 都不进 summary。代码切片里出现的项目专有标识符尽量泛化；这条规则的目的是让方法能在别的项目复用，带来源的总结会诱导下一次照搬而不判断。
- **只保留代码切片。** pattern 里用“修改前 / 修改后”的代码说明问题，不要大段复述代码逻辑；场景和要点用人话写。
- **不做性能测试。** 不写性能数字、加速比、cycle 数、吞吐、测试命令或“实测更快”这类结论。本模式只回答“怎么改”，性能验证由 optimize 模式或 origin 模式负责。
- **`summary/manual/` 是用户的。** 只在缺失时创建目录；已有文件不读、不改、不合并、不删除，也不要在 README 之外给它建索引文件。
- **合并只增不删。** 同一 slug 只追加场景/要点/示例；不要重排、覆盖或删除旧内容。方法确实写错时，在报告中说明并让用户确认，不要静默改历史。
- **逐函数核对。** 没读过前后两个函数体的候选不能写进总结；文件级 diff、函数名后缀、扫描器置信度都不能替代阅读。
- **宁缺毋滥。** 语义不等价、只是包装、只是重命名、只是换了编译分支的候选直接跳过，并在 `warnings` 里记一笔；不要为了凑数编造方法。
- **按代码实际 ISA 归类。** 文件名带 `sve` 不代表函数体用了 SVE；以 intrinsic/汇编助记符为准。反过来，NEON 实现里的 SVE 桥接代码要单独判断。
- **方法名稳定可复用。** slug 用 kebab-case，语义化，不带项目名和版本号；同一方法在不同批次出现时沿用已有 slug，否则合并脚本会生成重复条目。

## 参考资料

- `references/modes/summary/scan-playbook.md`：两种子模式的候选发现、ISA 识别特征、常见误配和分批策略；扫描结果不理想或候选很多时先读它。
- `references/modes/summary/method-taxonomy.md`：方法粒度、命名、合并/拆分判断和常见方法类别；写 bundle 前读它，避免同一机制被拆成多个条目。
- `references/modes/summary/output-contract.md`：bundle JSON 契约、`summary.md` 与 pattern 文件格式、合并与去重语义、失败路径；第一次写 bundle 前必读。
- `scripts/scan_simd.py`：候选发现；`scripts/merge_summary.py`：增量合并与导航重建；`scripts/validate_summary.py`：结构与禁用内容检查；`scripts/init_summary.py`：目录骨架。脚本只做确定性工作，语义判断仍由你完成。
