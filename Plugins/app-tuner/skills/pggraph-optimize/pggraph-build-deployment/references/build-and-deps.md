# 编译、依赖与产物

## 1. 依赖矩阵

| 依赖 | 版本/要求 | 说明 |
|------|-----------|------|
| PostgreSQL | 14–18 | 必须与 pgrx feature 一致；需要 `pg_config` 和服务端头文件（`postgres.h`） |
| Rust | 1.96.0 | 由 `graph/rust-toolchain.toml` 锁定；`rustc --version` 核对 |
| cargo-pgrx | 0.19.1 | `cargo install cargo-pgrx --version 0.19.1 --locked` |
| Python | ≥3.10 | 仅基准脚本需要（psycopg、数据转换） |
| zstd | 任意 | LDBC `.tar.zst` 解压 |
| Docker | 可选 | 官方 `run_benchmarks.sh` 默认 Docker；本机部署可绕过 |
| 编译器/链接器 | 系统工具链 | Linux aarch64/x86_64；Rust release 默认 LTO fat + codegen-units=1 |

Rust crate 版本由 `graph/Cargo.toml` 锁定（pgrx、roaring、xxhash-rust、bitvec、
bincode、crc32fast、thiserror、memmap2、serde、serde_json）。不要单独升级某个 crate
除非做兼容性验证。

## 2. 依赖检查

```bash
"${PGSQL_PREFIX}/bin/pg_config" --version
"${PGSQL_PREFIX}/bin/pg_config" --includedir-server
test -f "$( "${PGSQL_PREFIX}/bin/pg_config" --includedir-server )/postgres.h" && echo "headers ok"
rustc --version
cargo pgrx --version
python3 --version
zstd --version
```

若 `postgres.h` 缺失：

- Debian/Ubuntu：安装 `postgresql-server-dev-<major>`。
- 源码安装：确认 `--includedir-server` 指向实际头文件目录。

## 3. pgrx 初始化

pgrx 需要知道目标 PG 的 `pg_config`：

```bash
PG_MAJOR="$( "${PGSQL_PREFIX}/bin/pg_config" --version | sed -E 's/[^0-9]*([0-9]+).*/\1/' )"
PG_FEATURE="pg${PG_MAJOR}"
cargo pgrx init --"${PG_FEATURE}"="${PGSQL_PREFIX}/bin/pg_config"
```

只需一次；换 PG 版本或路径时重新初始化。

## 4. 编译安装

### 4.1 Makefile（推荐）

```bash
cd "${PGGraph_SRC}"
PG_CONFIG="${PGSQL_PREFIX}/bin/pg_config" make install
```

Makefile 内部大致执行：

```bash
cd graph && cargo pgrx install \
  --pg-config "${PG_CONFIG}" \
  --release \
  --no-default-features \
  --features "${PG_FEATURE}"
```

### 4.2 直接 cargo pgrx

```bash
cd "${PGGraph_SRC}/graph"
cargo pgrx install \
  --pg-config "${PGSQL_PREFIX}/bin/pg_config" \
  --release \
  --no-default-features \
  --features "${PG_FEATURE}"
```

`--no-default-features` 很重要：默认 feature 可能是别的 PG 版本；显式 `--features pgNN`
保证 SQL/ABI 与目标实例一致。

### 4.3 编译时间

release 默认 LTO fat + codegen-units=1，首次约数分钟；Rust 增量编译对源码小改更快，
但 `cargo pgrx install` 仍会重链接。时间不是故障信号。

## 5. 产物与安装位置

| 文件 | 目标路径 | 说明 |
|------|----------|------|
| `graph.so` | `pg_config --pkglibdir` | 扩展共享库 |
| `graph.control` | `pg_config --sharedir`/extension | 扩展控制文件 |
| `graph--0.1.8.sql` | `pg_config --sharedir`/extension | SQL 安装脚本（schema、函数、类型） |

核对：

```bash
"${PGSQL_PREFIX}/bin/pg_config" --pkglibdir
"${PGSQL_PREFIX}/bin/pg_config" --sharedir
ls -l "$( "${PGSQL_PREFIX}/bin/pg_config" --pkglibdir )/graph.so"
ls -l "$( "${PGSQL_PREFIX}/bin/pg_config" --sharedir )/extension/graph"*
sha256sum "$( "${PGSQL_PREFIX}/bin/pg_config" --pkglibdir )/graph.so"
```

记录 `.so` 的 SHA256 作为 A/B 身份；优化后对比同一路径的哈希。

## 6. 重编译与工件复用

1. 修改 `${PGGraph_SRC}` 源码。
2. `cargo pgrx install`（或 `make install`）。
3. 重启 PostgreSQL，让 backend 加载新 `.so`。
4. 若只改算法/常量、不动 `.pggraph` 格式：旧工件仍可 mmap，直接跑基准。
5. 若改了 CSR/工件格式：`graph.reset()` + 清 catalog + 重新 build；否则加载校验会失败。

“重启后首次查询慢”是正常的：每个 backend 首次 mmap + CRC + 重建反向 CSR。用预热
查询或持久连接把加载开销移出计时区间。

## 7. 常见编译错误

| 现象 | 原因 | 处理 |
|------|------|------|
| `postgres.h: No such file or directory` | 缺服务端头文件 | 安装 server-dev/确认 `--includedir-server` |
| `cargo pgrx install` feature 不匹配 | `pgNN` 与 `pg_config` 大版本不一致 | 重新推导 `PG_FEATURE`，加 `--no-default-features` |
| `cargo: command not found` | Rust 不在 PATH | `export PATH="${HOME}/.cargo/bin:${PATH}"` |
| `cargo pgrx` 未找到 | cargo-pgrx 未安装/版本不对 | `cargo install cargo-pgrx --version 0.19.1 --locked` |
| 链接错误/ABI 错误 | PG 头文件与运行库版本不一致 | 用同一 `pg_config` 编译和运行 |
| 安装后 `CREATE EXTENSION` 找不到 | 装到了另一个 PG 前缀 | 核对 `pg_config` 路径与 `pg_extension` 实例 |
| 编译很慢 | LTO/codegen-units | 正常；不要为跑分随意改 release profile，先记录 |

## 8. 安全与可复现

- 记录：`pgGraph` 版本/commit、Rust、pgrx、PG、编译命令、feature、`.so` SHA256。
- 不把密码、token、私钥写入脚本、argv、日志或仓库。
- 下载数据集后计算 SHA256 并记录；不要直接执行来源不明的脚本。
- 备份树只读，所有改动在工作树；需要回滚时从备份复制并重新编译。
