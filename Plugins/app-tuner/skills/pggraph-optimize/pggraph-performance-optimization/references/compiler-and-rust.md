# 编译与 Rust 优化

## 1. 默认 release 配置

pgGraph 源码的 release profile 通常是 LTO + 少量 codegen units（源资料提到 LTO fat +
codegen-units=1），因此：

- 首次编译/链接时间较长，属正常。
- 不要为了跑分随意改 `opt-level`/`lto`/`codegen-units`；如果改，必须单变量 A/B，
  并记录完整 profile。
- 优化后保留 `.so` SHA256，确认跑的是新二进制。

## 2. `target-cpu` 与 SVE

默认 target 是通用 `aarch64-unknown-linux-gnu`，通常只启用 NEON。可在
`graph/.cargo/config.toml` 设置：

```toml
[build]
rustflags = ["-C", "target-cpu=native"]
```

源资料实测（LDBC、A+B+C+H 基础上，40 次迭代；代号定义见 SKILL.md 速查表脚注：A=UnionFind
迭代化、B=node_deleted 快速路径、C=component_sizes Vec、H=traverse 排序键。注意本表是独立
测量系列，其基线 948.7ms 与 source-optimizations 默认轮次系列的 972ms 相差 2.4%，属轮次/
迭代数口径差，两系列不可横比）：

| 配置 | 启用的指令集 | TOTAL | 结论 |
|------|--------------|-------|------|
| generic | NEON | 948.7ms | 基线 |
| native-nosve | NEON + LSE/AES/CRC/FP16/... | 948.6ms | 与 generic 相同，无收益 |
| native-sve | generic + SVE/SVE2 | 925.2ms | -2.5% |

拆解：

- `target-cpu=native` 的非 SVE 部分（LSE/AES/CRC/FP16）在 pgGraph 中没有被编译器利用，
  收益 0%。
- SVE 自动向量化贡献全部约 2.5%：排序、计数、数组初始化等常规循环变宽。
- 手动 SVE gather（UnionFind find、is_active）是负收益，见
  `negative-results-and-decisions.md`。

## 3. 可移植性

`target-cpu=native` 在构建机器上生成该机器的指令集；把 `.so` 拷到不支持 SVE/SVE2 的
机器可能非法指令。生产/分发时：

- 明确构建机与运行机 CPU 特性一致；或
- 使用明确的 `target-cpu`/`target-feature`，并验证目标机支持；或
- 保持 generic，接受约 2.5% 的损失。

不要用 `native` 构建的 `.so` 做跨代际 CPU 对比。

## 4. 验证编译选项

```bash
# 1) rustflags 是否生效
grep -R "target-cpu\|rustflags\|lto\|codegen-units" "${PGGraph_SRC}/graph/.cargo" \
  "${PGGraph_SRC}/graph/Cargo.toml" 2>/dev/null

# 2) rustc 目标特性（构建机）
rustc --print target-features | grep -E 'sve|sve2|neon|lse|crc' | head

# 3) .so 中的指令计数（示例，按实际 objdump 支持调整）
# 与 verify_build_opts.sh 同口径：优先解析出的 PG_CONFIG（未设时回退 PGSQL_PREFIX 并校验可执行，
# 避免 PGSQL_PREFIX 为空时执行 /bin/pg_config、拼出 /graph.so）；OBJDUMP 缺失时提示安装
# llvm-objdump/objdump 或直接用脚本，不要对空命令串执行 -d
PG_CONFIG_BIN="${PG_CONFIG:-${PGSQL_PREFIX}/bin/pg_config}"
[ -x "${PG_CONFIG_BIN}" ] || { echo "ERROR: pg_config 不可用（设 PG_CONFIG 或 PGSQL_PREFIX）: ${PG_CONFIG_BIN}" >&2; exit 2; }
OBJDUMP="$(command -v llvm-objdump || command -v objdump || true)"
if [ -z "${OBJDUMP}" ]; then
  echo "ERROR: 未找到 llvm-objdump/objdump——安装后重试，或直接运行 scripts/verify_build_opts.sh" >&2
  exit 2
fi
"${OBJDUMP}" -d "$( "${PG_CONFIG_BIN}" --pkglibdir )/graph.so" \
  | grep -cE '\b(sve|ld1w|st1w|ptrue|whilelo|uaddv|smaxv)\b' || true

# 4) 身份
sha256sum "$( "${PG_CONFIG_BIN}" --pkglibdir )/graph.so"
```

`scripts/verify_build_opts.sh` 把这些检查串起来（只读）。

## 5. 对照实验设计

要区分“编译命令变化”与“SVE 本身”：

```
A: generic
B: target-cpu=native + target-feature=-sve,-sve2
C: target-cpu=native
```

- A vs B：其他 native 指令集的贡献。
- B vs C：SVE/SVE2 的贡献。
- 三者使用同一数据/查询/轮次/绑核。
- 用 `perf stat` 确认 IPC/指令数变化，用 `objdump` 确认 SVE 指令分布。

源资料结果：A≈B，C 比 B 快约 2.5%，说明收益来自 SVE 自动向量化，而不是 LSE/AES/CRC。

## 6. 编译优化适用条件

适合：

- 热点包含规则循环：排序、计数、数组初始化、批量比较。
- 构建机/运行机 CPU 一致，或能明确指定目标特性。
- 已通过 A/B 证明收益 > 噪声（>5% 才值得作为默认）。

不适合：

- 热点是不规则随机访问（UnionFind、BFS 邻居、位图检查）；SVE gather 往往更慢。
- 需要跨代 CPU 分发。
- 没有正确性门禁/回滚方案。

## 7. 记录模板

```
工具链: rustc 1.96.0, cargo-pgrx 0.19.1, PG 18.3
profile: release, lto=fat, codegen-units=1
rustflags: -C target-cpu=native
目标特性: neon, lse, aes, crc, fp16, sve, sve2
SVE 指令计数: ...
.so sha256: ...
对照: A/B/C, 轮次, 数据/查询/绑核
结果: TOTAL/QPS/每查询 median
结论: 保留/回滚/仅构建机使用
```
