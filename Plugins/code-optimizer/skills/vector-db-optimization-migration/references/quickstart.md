# 快速上手：一次最小可验收的向量数据库优化迁移

目标：在 30 分钟内完成“身份固定 → 版本审计 → 追溯矩阵 → patch 序列 → G0–G3 冒烟”，再进入完整 G4/G5。

> 命令中的 `<skill>` 指本 Skill 目录；所有产物写入用户指定的 `run_dir`，不要写回源码树。

## 0. 准备

```bash
export RUN_DIR=/path/to/run_dir
mkdir -p "$RUN_DIR"/{patches,G0-feature-off,G1-ut-st,G2-kernel-bitexact,G3-topk-dump,G4-recall,G5-perf}
cp <skill>/templates/SOURCE-TARGET-IDENTITY.json "$RUN_DIR/"
cp <skill>/templates/MIGRATION-MATRIX.md "$RUN_DIR/"
cp <skill>/templates/HANDOFF.md "$RUN_DIR/"
```

先填 `SOURCE-TARGET-IDENTITY.json`（含 `engine`）。身份字段缺任意一项，不开始迁移。

按 `engine-routing.md` 识别引擎并打开对应 adapter（Faiss → `engines/faiss.md`，DiskANN → `engines/diskann.md`）。adapter 未命中时只走通用流程，并把源码定位/基线/测试入口缺失列为 blocker 或 `DEFER`。

## 1. 版本差异审计

> 以下脚本适用于类 CMake 构建的引擎（如 Faiss）。DiskANN 等 Rust/自建构建引擎改用其 adapter 指定的构建取证方式。

```bash
python3 <skill>/scripts/scan_version_delta.py \
  --source /path/to/source_engine \
  --target /path/to/target_engine \
  --json "$RUN_DIR/version-delta.json"
```

看三件事：

- 源有、目标无的 CMake option / 宏（可能是死定义或需要重写）。
- 目标新增/删除的公共头（ABI 风险）。
- 公共头内容变化（重构建影响所有 TU）。

审计结论按 `version-delta-audit.md` 的 8 个类别逐项记录；不能用版本号印象代替代码事实。

## 2. 构建与宏一致性

```bash
python3 <skill>/scripts/check_compile_flags.py \
  --compile-commands /path/to/build/compile_commands.json \
  --path-filter <相关源码子串，如 faiss/> \
  --require-macro <MACRO=VALUE> \
  --require-flag=-march=armv8.2-a+sve \
  --json "$RUN_DIR/compile-flags.json"
```

要求：相关 TU 的 `-D`、`-march`、`-flto` 一致；脚本默认“缺失或分裂即退出 1”（`-U` 取消的宏算缺失，含空格形态 `-U NAME`；某 TU 缺 `-march` 算分裂；`-march val` 空格形态与 `-march=val` 等价识别；`-fno-lto` 等负向 flag 记为显式关闭，与开启、缺失都判定为不同取值）。缺项或分裂时先修构建，不进入门禁。用 `--path-filter` 限定到迁移涉及的 TU，避免对全量 TU（如纯 C 文件）误报；确需只报告不失败时显式加 `--allow-inconsistent`。

## 3. 追溯矩阵与 patch 排序

按 `migration-contract.md` 填 `MIGRATION-MATRIX.md`：

- 每个机制一行，写 `path::symbol`，行号只作辅助并绑定 commit。
- 先按 `底座 → 结构使能 → 跨算法共享宏 → 算法专属宏` 排序。
- 每个 patch 只承载一个机制，保留 feature-off。

建议第一轮只选 1–2 个低风险 `KEEP` 机制做纵向打通，不要一次铺满。

## 4. G0：feature-off 等价

1. 关闭全部迁移宏，干净重建。
2. 用同一索引、同一查询、同一参数跑 search。
3. dump top-k，逐字节比较标签与距离。

```bash
python3 <skill>/scripts/compare_topk.py \
  --baseline "$RUN_DIR/G0-feature-off/base.dump" \
  --candidate "$RUN_DIR/G0-feature-off/off.dump" \
  --k 10
```

脚本默认严格：查询集（双向）、每查询条数、rank 连续无重复、doc-id 顺序、按 doc-id 对齐的距离**按位模式**全等（`-0.0` 与 `+0.0` 也不放过；零容差）任一不符即退出 1；省略 `--k` 时全量比对（候选多出的行不会被静默截掉），显式 `--k` 才截断两侧。不要为“通过”放宽容差；只有 OPT_IN 机制按其契约显式加 `--allow-order-change`（仍要求集合与距离全等）或显式 `--atol/--rtol` 并记录理由（非零容差下比较退化为数值相等，±0.0 不再区分）。

任何差异都停止，先排查宏/ABI/构建，不要进入性能阶段。

## 5. G1–G3 冒烟

- G1：跑目标版本 UT/ST，feature-on/off 各一轮；记录用例数、通过/跳过/失败与原始日志。
- G2：用 `templates/kernel_probe.cpp` 做双库 dlopen 位级探针（先改符号签名），覆盖 metric、维度、batch 尾部。
- G3：迁移库与目标基线库各跑一遍 search，dump top-k 后用 `compare_topk.py` 比较。

```bash
g++ -O2 -std=c++17 <skill>/templates/kernel_probe.cpp -ldl -o "$RUN_DIR/G2-kernel-bitexact/probe"
"$RUN_DIR/G2-kernel-bitexact/probe" <baseline.so> <candidate.so> <symbol>
```

## 6. G4–G5（完整迁移）

- G4：每数据集 recall@k 不得低于冻结基线（含 1e-6 级微差）。recall 必须用冻结 ground truth 对每臂分别计算；`compare_topk.py` 的 `overlap@k` 是候选与基线 top-k 的集合重合率，只作等价性排查参考，不得当 recall 报告。同时看逐查询 mismatch。
- G5：同机、同窗口、同核集/NUMA、同索引做交错 ABBA；报告每数据集 QPS、geomean、per-dataset floor、噪声地板与归因。

不要把 G0–G3 的冒烟结果当作最终收益；也不要在 G4 失败时进入 G5。

## 7. 裁决与交接

- 每机制给 `KEEP/ADAPT/SPLIT/OPT_IN/REPLACE/DROP/DEFER` + 门禁状态。
- 在 `HANDOFF.md` 记录 open items 的触发条件与复测门槛。
- 把目标版本的新事实回写到对应算法 Skill 的 experiments reference，并带证据标记。

## 最小完成定义

- 身份卡完整；
- 追溯矩阵无空行；
- patch 可按序 `git apply --check`；
- G0 位级一致；
- G1 通过（feature-on/off）；
- G2/G3 覆盖目标热路径且无 mismatch；
- G4 recall 零下降；
- G5 同窗 ABBA 有原始日志与噪声说明。
