# 固定提交的 BP 最小操作示例

## 版本、前提和实现边界

固定上游提交为 `123245f2085a164b1e70751eb784aa77046dab46`。已核对该提交的 [workspace Cargo.toml](https://github.com/lancedb/lance/blob/123245f2085a164b1e70751eb784aa77046dab46/Cargo.toml)、[crate 特性及 benchmark 注册](https://github.com/lancedb/lance/blob/123245f2085a164b1e70751eb784aa77046dab46/rust/lance-encoding/Cargo.toml) 和 [decoder.rs](https://github.com/lancedb/lance/blob/123245f2085a164b1e70751eb784aa77046dab46/rust/lance-encoding/benches/decoder.rs)。该提交要求 Rust 至少 1.95.0；还需满足仓库的构建依赖。

上游提供 `bitpacking` 特性、默认 bench profile（优化并保留调试信息），以及下述两个 BP 基础用例。上游不提供本示例的 `experimental-bp-neon` 特性或审计测试；这些属于另行准备的候选补丁。本 skill 不附带 Lance 内核补丁。原版部分可独立操作，候选部分需要针对该提交的补丁；缺少补丁时记录 `awaiting_candidate_patch`。

以下命令从 Bash 执行，示例目录应为新目录。用于目标 AArch64 NEON 环境的操作记录；本次只核对源码，构建、ARM 测试、命中与性能均为 `awaiting_target_evidence`。

## 1. 固定上游并列出官方用例

```bash
set -euo pipefail
git init lance-bp-repro
cd lance-bp-repro
git remote add origin https://github.com/lancedb/lance.git
git fetch --depth=1 origin 123245f2085a164b1e70751eb784aa77046dab46
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = 123245f2085a164b1e70751eb784aa77046dab46
mkdir -p ../bp-evidence
rustc --version > ../bp-evidence/rustc.txt
cargo --version > ../bp-evidence/cargo.txt
CARGO_TARGET_DIR="$PWD/target/bp-baseline" cargo bench --locked \
  -p lance-encoding --bench decoder --features bitpacking -- \
  'decode_inline_bitpacking_unchunk/' --list \
  | tee ../bp-evidence/baseline-list.txt
```

列表应包含 `u32_bw12_1024` 和 `u64_bw23_1024`，各自带 `legacy_copy/compressed_bytes`、`typed_view/compressed_bytes` 后缀。若缺失或仅列出特性关闭的占位项，停止测量并核对提交和特性。`bitpacking` 只启用上游 BP，不证明 NEON 命中。

## 2. 先保存同包装基线

```bash
CARGO_TARGET_DIR="$PWD/target/bp-baseline" cargo bench --locked \
  -p lance-encoding --bench decoder --features bitpacking -- \
  'decode_inline_bitpacking_unchunk/(u32_bw12_1024|u64_bw23_1024)/typed_view/compressed_bytes' \
  | tee ../bp-evidence/baseline.txt
```

只比较同名 typed_view 路径。这个计时包含其包装及完整入口工作，不是纯 NEON 内核。该命令只示范一轮；正式结论仍按主文的交错多轮协议采集。

## 3. 候选补丁、构建开关和正确性审计

为使例子具体，候选补丁需提供以下接口约定；它们不是上游原生接口，也不是自动适用于任意补丁的名字：

- Cargo 特性 `experimental-bp-neon = ["bitpacking"]`，关闭时保留原版入口。
- 仅在 AArch64 且 NEON 可用、类型为 u32/u64、位宽在中间范围、物理块与容量合法时进入候选；返回不适用前无写入。
- 名称带 `test_experimental_bp_neon_u32_entry`、`test_experimental_bp_neon_u64_entry` 的集成测试，分别覆盖所有位宽及逻辑长度 1、1023、1024。测试在正常 block/miniblock 入口逐值核对结果，并断言真正执行候选的计数。

先提供补丁的绝对路径，检查适用性后应用；记录补丁内容和摘要。若补丁使用其他名字或适用位宽，应同步替换命令、命中表与审计断言，不凭空添加运行时环境开关。

```bash
: "${BP_CANDIDATE_PATCH:?请设置针对固定提交的候选补丁绝对路径}"
git apply --check "$BP_CANDIDATE_PATCH"
git apply "$BP_CANDIDATE_PATCH"
git diff --check
cp "$BP_CANDIDATE_PATCH" ../bp-evidence/candidate.patch
sha256sum ../bp-evidence/candidate.patch > ../bp-evidence/candidate.sha256
git status --short > ../bp-evidence/candidate-status.txt
cargo metadata --locked --no-deps --format-version 1 > ../bp-evidence/candidate-metadata.json
CARGO_TARGET_DIR="$PWD/target/bp-candidate" cargo test --locked \
  -p lance-encoding --features experimental-bp-neon \
  test_experimental_bp_neon_ -- --list \
  | tee ../bp-evidence/candidate-tests-list.txt
CARGO_TARGET_DIR="$PWD/target/bp-candidate" cargo test --locked \
  -p lance-encoding --features experimental-bp-neon \
  test_experimental_bp_neon_ -- --nocapture \
  | tee ../bp-evidence/candidate-tests.txt
```

保存输入补丁本身可以保留其中新增文件的内容；仅运行 `git diff` 会漏掉尚未跟踪的新内核文件，不能据此记录完整候选身份。先确认 metadata 中确实定义该特性，测试列表非空且同时覆盖 u32/u64；Cargo 成功但运行零个测试不算通过。

| 输入 | 上述补丁约定的预期路径 |
| --- | --- |
| u32，位宽 1–31，包括官方 bw12 | 进入 NEON |
| u64，位宽 1–63，包括官方 bw23 | 进入 NEON |
| 零宽、满宽、其他类型 | 原版回退 |
| 特性关闭或非目标平台 | 原版，不执行 NEON |

若一次审计分别调用 block 和 miniblock，对每个适用位宽和合法逻辑长度应累计 2 次候选调用，零宽和满宽为 0；逻辑尾长不改变 1024 元素物理输出空间的要求。计数断言属于候选测试的实现要求，表格不是已获得的实机成绩。

## 4. 关闭审计并测同名候选

确保审计计数仅在测试构建启用，正式 benchmark 保留准入判断、关闭计数，再执行：

```bash
CARGO_TARGET_DIR="$PWD/target/bp-candidate" cargo bench --locked \
  -p lance-encoding --bench decoder --features experimental-bp-neon -- \
  'decode_inline_bitpacking_unchunk/(u32_bw12_1024|u64_bw23_1024)/typed_view/compressed_bytes' \
  | tee ../bp-evidence/candidate.txt
```

这是用两个构建目录按 Cargo 特性选择原版/候选的例子。除候选实现及其必要接入外，需保持相同包装、数据、工具链与构建参数；若补丁还修改其他路径，先消除或单独说明混杂因素。保留源码、补丁及二进制身份、实际命中、正确性结果、CPU/NUMA 和原始轮次；没有目标执行记录时不填写加速比。
