# 源码编译与安装（REL_17_5 / REL_18_3）

> 主动执行类。所有命令经变量注入；版本锚点：官方 tag
> `REL_17_5` / `REL_18_3`（`git -C ${PG_SRC} rev-parse REL_17_5` 可核验）。

## 1. 依赖（openEuler / aarch64）

```bash
# 必需工具链与依赖（bison/flex 缺失会在 configure/make 阶段报错，预检先行）
need_cmd() { command -v "$1" >/dev/null 2>&1 || echo "缺少: $1"; }
for c in gcc make bison flex perl tar; do need_cmd "$c"; done
need_cmd "${PG_CONFIG:-pg_config}" 2>/dev/null || true

# readline/zlib 开发头 + §2 默认 configure 的三个依赖（缺任一都会在 configure 报错）
# openEuler: dnf install -y readline-devel zlib-devel openssl-devel libicu-devel llvm-devel
```

- ICU（`--with-icu`）与 LLVM（`--with-llvm`，JIT）按需启用；不加也能编译，
  但 glibc 排序口径与 JIT 能力随之缺失——按目标口径选择并记入 configure 串。
- 编译用户非 root；安装前缀目录属主先行规划。

## 2. 基础编译与安装

```bash
cd "${PG_SRC}"
git status --short            # 打过补丁的树先记录状态，便于回退
./configure --prefix="${PG_INSTALL}" \
            --with-openssl \
            --with-icu \
            --with-llvm       # 按需增减；整串会被 pg_config --configure 复述
make -j"$(nproc)"
make install
```

- 安装布局：`${PG_INSTALL}/{bin,lib,share}`；扩展随 `make install` 落
  `share/extension` + `lib`。
- 并行度：`make -j$(nproc)` 全核；内存不足时降到物理核数一半。

## 3. ARM 优化构建（鲲鹏口径）

| 选项 | 作用 | 风险与回退 |
|------|------|-----------|
| `-march=armv8.2-a+...`（按机型） | 启用目标机指令 | 交叉可移植性下降：二进制换机型需重编；回退=重新 configure 默认 march |
| LTO（`CFLAGS="-flto"`，配 `--with-llvm` 可用ThinLTO） | 跨翻译单元内联 | 编译时间上升；火焰图帧名带 `.lto_priv`、顶层出现归因伪影（如 `GetCommandTagEnum` 实为 PostgresMain 主循环的 LTO 归因），解读火焰图时须知道构建口径 |
| LSE 原子 | ARM 大核原子指令更快 | 老机型不支持则 illegal instruction；`-moutline-atomics`（运行时按 CPU 选择 LSE/LLSC 路径）是兼容折中 |

```bash
# 优化构建示例（记录整串进验收输出，勿凭记忆复述）
./configure --prefix="${PG_INSTALL}" --with-openssl --with-icu \
            CFLAGS="-O2 -flto -march=armv8.2-a -moutline-atomics"
make -j"$(nproc)" && make install
```

- 判别产物是否带 LSE：`objdump -d "${PG_BIN}/postgres" | awk '$3 ~ /^(ldadd|cas|swp|ldclr|ldeor)[abhl]*$/' | wc -l`
  —— 匹配反汇编**助记符列**（$3）并覆盖合法后缀（a=acquire、l=release、al 组合、
  b/h=字节/半字尺寸变体）：只生成 `casal`/`ldaddal` 等带内存序变体的产物不会被
  词边界规则漏判为「无 LSE」；计数为 0 才解释为「未生成 LSE 路径」——outline-atomics
  构建运行时才选路径，静态计数不可作 LSE 生效与否的最终判定（判读口径见
  `../../pgsql-performance-optimization/references/compiler-flags-and-verification.md`）。
  （ARM LSE 指令族；计数为 0 说明未生成 LSE 路径，见
  `../../pgsql-performance-optimization/references/compiler-flags-and-verification.md`）。

## 4. 安装验收（不进数据库，只查产物）

```bash
"${PG_CONFIG}" --version          # 期望 postgresql 17.5 / 18.3 对应版本串
"${PG_CONFIG}" --configure        # 与本次 configure 串一致（含 CFLAGS）
"${PG_CONFIG}" --pkglibdir        # 扩展模块目录
"${PG_CONFIG}" --libdir           # 普通链接库目录
md5sum "${PG_BIN}/postgres"       # 身份记录进验收输出
nm "${PG_BIN}/postgres" | grep -cF "PostgresMain"   # 符号存在性（字面匹配，非正则）
```

- 符号核对用 `grep -F`（字面）：正则模式下 `operator[]` 这类合法符号会被解析成
  空字符类报错（nebula 家族同款教训）。
- LTO 构建的符号仍可见（非 strip）；若产物被 strip，符号证据记“不可用”，不要
  用其他符号冒充。

## 5. 版本切换（源码多版本并存）

```bash
# 检出目标版本（可核验锚点）
git -C "${PG_SRC}" fetch --tags origin
git -C "${PG_SRC}" checkout REL_17_5   # 或 REL_18_3；checkout 前先 git status 确认补丁状态
```

- 每版本独立 `--prefix`（如 `${PG_INSTALL}-17.5`），不要互相覆盖；
- 大版本切换涉及 on-disk catalog 格式：**数据目录不通用**，必须 initdb 新目录
  + 逻辑导出/升级工具迁移（pg_upgrade 走 build-deployment 之外的专项流程）；
- 小版本（17.5→17.x）二进制可替换但按 §4 验收 + 冒烟后才能放量。
