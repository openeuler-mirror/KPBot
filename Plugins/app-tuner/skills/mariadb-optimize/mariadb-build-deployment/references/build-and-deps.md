# 构建、依赖与编译选项

本文件给出 MariaDB 10.11.16 的可复现构建路径、三方依赖作用、LSE/jemalloc/tcmalloc
的正确用法，以及常见编译错误。

## 1. 系统依赖

```
# openEuler / CentOS / RHEL 系（包名以仓库实际为准）
yum -y install \
  gcc gcc-c++ make cmake bison \
  ncurses-devel readline-devel \
  openssl-devel openssl-libs \
  zlib-devel libaio-devel libxml2-devel \
  lz4 lz4-devel \
  libcurl-devel libtool \
  perl perl-Data-Dumper python3 \
  numactl numactl-devel \
  pkgconfig
```

> NUMA 开发包：openEuler/CentOS/RHEL 用 `numactl-devel`（含 `pkgconfig(numa)` 与头文件），
> Debian/Ubuntu 系用 `libnuma-dev`。仓库中不存在 `libnuma-devel`，一条 `yum -y install`
> 中任一包 No match 会导致整条命令不装任何包。

按需补充：

- `gnutls-devel`：使用 GnuTLS 而非 OpenSSL 时。
- `boost`：部分组件/工具需要。
- `jemalloc`/`tcmalloc`：性能对照时才需要，见 §4。
- `valgrind`/`gdb`/`perf`：调试与热点分析。
- `ninja-build`：可选加速构建。
## 2. CMake 选项（10.11）

| 选项 | 作用 | 建议 |
| --- | --- | --- |
| `-DCMAKE_BUILD_TYPE=Release` | 优化级别与断言 | 性能测试必须 Release |
| `-DCMAKE_INSTALL_PREFIX` | 安装前缀 | `${MARIADB_HOME}` |
| `-DSYSCONFDIR` | 默认配置目录 | `/etc` |
| `-DMYSQL_DATADIR` | 默认数据目录 | `${DATADIR}` |
| `-DMYSQL_UNIX_ADDR` | 默认 socket | `${SOCKET}` |
| `-DWITH_INNOBASE_STORAGE_ENGINE=1` | InnoDB | 必开 |
| `-DWITH_ARCHIVE_STORAGE_ENGINE=1` | ARCHIVE | 可选 |
| `-DWITH_BLACKHOLE_STORAGE_ENGINE=1` | BLACKHOLE | 可选 |
| `-DWITH_PERFSCHEMA_STORAGE_ENGINE=1` | performance_schema | 需要 PFS 时开 |
| `-DWITH_SSL=system` | 使用系统 OpenSSL | 推荐 |
| `-DWITH_ZLIB=system` | 使用系统 zlib | 推荐 |
| `-DDEFAULT_CHARSET` / `-DDEFAULT_COLLATION` | 默认字符集 | `utf8mb4` / `utf8mb4_general_ci` |
| `-DCMAKE_C_FLAGS` / `-DCMAKE_CXX_FLAGS` | 优化/架构参数 | 见 §3 |
| `-DCMAKE_EXE_LINKER_FLAGS` | 链接参数（jemalloc） | 见 §4 |
| `-DWITH_DEBUG=0` | 关闭调试断言 | 性能测试 |

> 旧文档里的 `-DWITHOUT_TOKUDB=1`（10.3 时代）在 10.11 已无对应引擎，照抄可能报
> “unknown variable”；配置前用 `cmake -LH` 或 `CMakeLists.txt` 核对当前版本选项。

## 3. 架构与 LSE

ARMv8.1 的 LSE 原子指令（`CAS`、`LDADD`、`SWP` 等）可替代 `LDREX/STREX` 循环，
在多核竞争下减少重试。本 skill 同时支持 x86_64，通用构建路径**不得**无条件传入 ARM
专用 `-march=armv8.2-a+lse`（x86 GCC 会报 unrecognized command line option）。先按架构
生成平台适配的编译参数：

```
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) CFLAGS_BASE="-O2" ;;                 # ARM64 默认目标，可移植
  x86_64|amd64)  CFLAGS_BASE="-O2 -march=native" ;;   # x86 平台适配参数，勿传 ARM -march
  *)             CFLAGS_BASE="-O2" ;;
esac
# C/C++ 通用编译参数：CFLAGS_BASE="-O2"（x86 可按需加 -march=native/-mavx2）
```

仅在**确认目标 ARM CPU 支持 LSE**（且目标负载 A/B 有收益）时，才在 aarch64 上追加：

```
# 只用于 aarch64 且目标 CPU 支持 LSE 的场景
CFLAGS_BASE="${CFLAGS_BASE} -march=armv8.2-a+lse"
# 对应 cmake 参数：
# -DCMAKE_C_FLAGS="${CFLAGS_BASE}" -DCMAKE_CXX_FLAGS="${CFLAGS_BASE}"
```

注意：

- 目标 CPU 必须支持 LSE，否则运行时非法指令；跨机型分发前确认。
- 鲲鹏 4P 案例中 C3（TPC-C）场景的 CAS 是热点，启用 LSE 有实测收益；
但同一资料也指出“某些场景 LSE 会劣化”，所以必须用目标负载做 A/B，不能默认全场景更好。
- x86_64 无 LSE，可用 `-march=native` 或 `-mavx2` 等，但与 LSE 无可比性。

## 4. 分配器：jemalloc 与 tcmalloc

### 4.1 构建期链接 jemalloc

```
yum -y install jemalloc jemalloc-devel
# 在 CMake 配置中加入
-DCMAKE_EXE_LINKER_FLAGS="-ljemalloc"
```

验证：

```
ldd "${MARIADB_HOME}/bin/mariadbd" | grep jemalloc
"${MARIADB_HOME}/bin/mariadbd" --version
```

### 4.2 运行期 LD_PRELOAD tcmalloc

```
# 编译 gperftools（示例版本 2.7）
tar -zxf gperftools-2.7.tar.gz
cd gperftools-2.7
./configure --enable-frame-pointers
make -j"$(nproc)" && make install

# 通过 systemd 或启动脚本注入（不要写死在仓库脚本里）
# Environment="LD_PRELOAD=/usr/local/lib/libtcmalloc.so"
```

验证：

```
lsof -p "$(pgrep -x mariadbd | head -1)" | grep -Ei 'tcmalloc|jemalloc'
```

纪律：

- **只选一种**分配器；同时链接 jemalloc 又 LD_PRELOAD tcmalloc 会行为不确定。
- 分配器收益依赖分配模式；必须与基线同编译参数、同数据、同压测口径 A/B。
- `--enable-frame-pointers` 有利于 perf 栈回溯，但可能略微影响性能，需记录。

## 5. 构建流程

```
cd "${SRC_ROOT}"

# 构建目录默认本轮新建的空目录：不复用、不清理任何既有目录（无 rm -rf 分支）
BUILD_DIR="${BUILD_DIR:-${SRC_ROOT}/build_$(date +%Y%m%d_%H%M%S)}"
if [ -e "${BUILD_DIR}" ]; then
  # 既有目录只做校验与判空：拒绝符号链接、拒绝源码树外路径，非空即退出，不删除任何文件
  [ -L "${BUILD_DIR}" ] && { echo "拒绝符号链接 BUILD_DIR" >&2; exit 2; }
  B_REAL="$(realpath "${BUILD_DIR}")"
  S_REAL="$(realpath "${SRC_ROOT}")"
  [ "${B_REAL}" != "${S_REAL}" ] || { echo "BUILD_DIR 不得为源码根目录" >&2; exit 2; }
  case "${S_REAL}/" in "${B_REAL}/"*) echo "BUILD_DIR 不得为源码树的上级目录" >&2; exit 2 ;; esac
  # 关键：必须位于源码树之内，防止越界命中源码树外的 build_* 用户目录
  case "${B_REAL}/" in "${S_REAL}/"*) ;; *) echo "BUILD_DIR 必须位于源码树内" >&2; exit 2 ;; esac
  case "$(basename "${B_REAL}")" in build_*) ;; *) echo "BUILD_DIR 须为本轮专用的 build_<ts> 目录" >&2; exit 2 ;; esac
  [ -z "$(ls -A "${B_REAL}")" ] || { echo "BUILD_DIR 非空：不清理、不复用，请换用新的 build_<ts> 目录" >&2; exit 2; }
fi
mkdir -p "${BUILD_DIR}" && cd "${BUILD_DIR}"

# 平台适配参数（详见 §3）：x86 用 -march=native，默认不传 ARM 专有 -march
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) CFLAGS_BASE="-O2" ;;                 # ARM64 默认目标，可移植
  x86_64|amd64)  CFLAGS_BASE="-O2 -march=native" ;;
  *)             CFLAGS_BASE="-O2" ;;
esac
# 仅当目标 ARM CPU 确认支持 LSE（且负载 A/B 有收益）时才追加：
# CFLAGS_BASE="${CFLAGS_BASE} -march=armv8.2-a+lse"

cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="${MARIADB_HOME}" \
  -DSYSCONFDIR=/etc \
  -DMYSQL_DATADIR="${DATADIR}" \
  -DMYSQL_UNIX_ADDR="${SOCKET}" \
  -DWITH_INNOBASE_STORAGE_ENGINE=1 \
  -DWITH_ARCHIVE_STORAGE_ENGINE=1 \
  -DWITH_BLACKHOLE_STORAGE_ENGINE=1 \
  -DWITH_SSL=system \
  -DWITH_ZLIB=system \
  -DDEFAULT_CHARSET=utf8mb4 \
  -DDEFAULT_COLLATION=utf8mb4_general_ci \
  -DCMAKE_C_FLAGS="${CFLAGS_BASE}" \
  -DCMAKE_CXX_FLAGS="${CFLAGS_BASE}"

make -j"$(nproc)"
make install
```

产物核对：

```
ls -lh "${MARIADB_HOME}/bin/"{mariadbd,mariadb,mariadb-install-db,mariadb-backup}
"${MARIADB_HOME}/bin/mariadbd" --version
md5sum "${MARIADB_HOME}/bin/mariadbd"
```

## 6. 常见编译错误

| 错误 | 原因 | 处置 |
| --- | --- | --- |
| `Could NOT find Curses` | 缺 ncurses | 装 `ncurses-devel` |
| `Could NOT find OpenSSL` | 缺开发包或路径 | 装 `openssl-devel`；指定 `-DWITH_SSL=system` |
| `Bison 3.x required` | bison 版本低 | 升级 bison |
| `cannot find -ljemalloc` | 缺 jemalloc-devel | 装包或去掉链接 |
| `undefined reference to numa_*` | 缺 NUMA 开发库 | openEuler/CentOS/RHEL 装 `numactl-devel`，Debian/Ubuntu 装 `libnuma-dev` |
| `internal compiler error` | 编译器/内存不足 | 降 `-j`、升高内存、换 GCC 版本 |
| `relocation R_AARCH64_ADR_PREL_PG_HI21` | 链接/位置无关冲突 | 检查 LTO/PIC 组合与工具链 |
| `fatal error: lz4.h: No such file` | 缺 lz4 开发包 | 装 `lz4-devel` |
| `error: unrecognized command line option '-march=armv8.2-a+lse'` | x86 上传了 ARM `-march`，或编译器过旧 | 按 §3 用平台适配参数；aarch64 确认支持 LSE 后再启用，必要时升级 GCC（≥8/9） |
| 安装后启动 `unknown variable` | 旧 my.cnf 含已移除参数 | 按 10.11 参数表清理 |

## 7. 构建身份记录（必须）

每次性能对照保存：

```
源码 commit / tag：
CMAKE_BUILD_TYPE：
C/CXX flags：
链接器 flags（jemalloc 等）：
编译器版本：
mariadbd md5：
依赖版本（OpenSSL/zlib/lz4/jemalloc）：
```

没有这些信息，任何“提升 X%”都不可复核。

## 8. 交叉编译提示

- aarch64 原生构建最简单；交叉编译需准备 sysroot 与目标架构的 OpenSSL/zlib。
- 交叉编译的二进制必须在目标机型做启动与 LSE 指令检查：

```
# 按反汇编助记符列（$3）匹配，覆盖 casal/ldaddal/swpal 等内存序后缀，
# 并覆盖 ldclr/ldeor/ldset/ldsmax/ldsmin/ldumax/ldumin 及 st 前缀族
# （与姊妹 skill mariadb-performance-optimization 的 verify_build_opts.sh §1.2 同口径）。
DIS="$(mktemp)" || exit 1
if objdump -d "${MARIADB_HOME}/bin/mariadbd" >"$DIS" 2>/dev/null; then
  LSE_CNT="$(awk '$3 ~ /^(cas|ldadd|ldclr|ldeor|ldset|ldsmax|ldsmin|ldumax|ldumin|swp|stadd|stclr|steor|stset|stsmax|stsmin|stumax|stumin)[a-z]*$/' "$DIS" | wc -l)"
  echo "LSE 指令命中：${LSE_CNT}"
  [ "${LSE_CNT}" -gt 0 ] || echo "扫描成功但零命中（可能未启用 -march=...+lse，或工具链未生成）"
else
  echo "objdump 扫描失败（无法反汇编），不能按零命中判定" >&2
fi
rm -f "$DIS"
```

> 指令计数只是存在性检查，不代表热点路径命中；收益仍需目标负载实测。
