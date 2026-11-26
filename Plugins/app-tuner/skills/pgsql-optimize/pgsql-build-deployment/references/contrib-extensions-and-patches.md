# contrib 扩展开发与补丁管理

> 主动执行类。扩展安装会写 `${PG_INSTALL}/share/extension` 与 `lib/`，属变更操作；
> 补丁操作前先 `--check`。

## 1. contrib 开发模型

- contrib 扩展 = PG 源码树 `contrib/<name>/` 下的 PGXS 模块：`Makefile` 声明
  `MODULES`/`MODULE_big`/`EXTENSION`/`DATA`，`make install` 后经
  `CREATE EXTENSION` 在库内启用。
- 自研扩展不入源码树也可走 PGXS：`PG_CONFIG=` 指向目标安装的 pg_config，
  `make install` 落同一布局。
- 观测型扩展（读共享内存统计）通常需要 `shared_preload_libraries` 预载并在
  postmaster 阶段申请共享内存——**改后必须 restart 才生效，reload 无效**。

```bash
# PGXS 独立目录编译（不进源码树）
cd "${EXT_SRC}"                     # 扩展源码目录，含 Makefile/控制文件
make PG_CONFIG="${PG_CONFIG}"
make PG_CONFIG="${PG_CONFIG}" install
"${PG_BIN}/psql" -h "${PGHOST}" -p "${PG_PORT}" -U "${PGUSER}" -d "${DBNAME}" \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_buffer_stats;'
# 若扩展声明了 shared_preload_libraries 需求：
#   postgresql.conf: shared_preload_libraries = 'pg_buffer_stats'
#   然后必须 restart（不是 reload）：pg_ctl restart -D "${PGDATA}" -m fast
```

## 2. 内置观测扩展速览（安装口径）

| 扩展 | 是否需 shared_preload_libraries | 安装后入口 |
|------|--------------------------------|-----------|
| pg_stat_statements | 是（需 restart） | 视图 `pg_stat_statements`（`pg_stat_statements_reset()` 清零） |
| pg_buffercache | 否（CREATE EXTENSION 即可，建议预载稳视图） | 视图 `pg_buffercache` |
| pgstattuple / pg_visibility / pg_freespacemap | 否 | 函数族 `pgstattuple(...)` 等 |
| auto_explain | 否（两种加载方式，见下注） | 日志输出执行计划 |
| 自研 pg_buffer_stats | 是（自研观测扩展） | 视图 `pg_buffer_stats.hotspots`、`cas_hotspots`、函数 `analyze_distribution()`/`performance_summary()`/`reset_stats()` |

- auto_explain 加载方式二选一：**会话级**（超级用户 `LOAD 'auto_explain';` 后设置
  `auto_explain.log_min_duration` 即可采集，无需重启，适合单会话诊断）；**全局预载**
  （`shared_preload_libraries='auto_explain'` + restart，适合持续采集/非超级用户
  会话）。表中其余「是（需 restart）」的扩展（pg_stat_statements、自研
  pg_buffer_stats）没有会话级 LOAD 路径，勿混淆适用场景；
- 使用入口与判读口径归 `../../pgsql-test-verification/references/observation-extensions.md`；
- A/B 对比轮次两侧观测扩展开关必须一致（观测自身有 CPU 成本，
  pgss_store 实测 ~1.8-5% 口径见架构参考）。

## 3. 自研扩展（pg_buffer_stats 形态）

- 目的：把缓冲区锁路径的内部竞争（CAS 重试、分区锁等待）变成可查询的共享统计
  （`hotspots`/`cas_hotspots` 视图），补齐内置工具看不到的 off-CPU/自旋证据。
- 实现要点（架构对齐见 `../../pgsql-architecture/references/buffer-manager-and-locks.md`）：
  统计点放在 LWLock 尝试/CAS 重试路径上（对应 `LWLockAttemptLock` 一带，以版本
  源码为准）；共享计数经 `ShmemInitHash`/共享数组申请；GUC 开关默认 off
  （`buffer_stats_enabled`，自研扩展自带），开销目标 <1%。
- 交付形态：源码目录（Makefile + .c/.h + 控制文件 + SQL 文件）；打补丁进源码树或
  PGXS 独立编译均可——二选一并记录口径。

## 4. 补丁管理与回退

```bash
# ① 先检查（干净跟踪树 + 补丁可应用，任何一步失败不硬上）
cd "${PG_SRC}"
git status --short                        # 应为干净（只看已跟踪改动）；有未提交改动先处理
# 补丁原件必须在源码树外（PATCH_DIR 默认 ${PG_INSTALL}/patches，不在 ${PG_SRC} 内）
test -f "${PATCH_DIR}/num_buffer_partitions.patch" \
  || { echo "补丁原件缺失：${PATCH_DIR}/num_buffer_partitions.patch，禁止继续" >&2; exit 2; }
git apply --check "${PATCH_DIR}/num_buffer_partitions.patch" \
  || { echo "补丁不适用（上下文偏移？），勿强行应用" >&2; exit 2; }

# ② 应用并记录（审计记录写源码树外，避免被清理误删；不用 tee——管道会吞 git diff 退出码）
git apply "${PATCH_DIR}/num_buffer_partitions.patch"
git diff --stat > "${PATCH_DIR}/applied_$(date +%Y%m%d_%H%M%S).log"

# ③ 重编重装（同 §build-from-source）——从干净构建产物重编：本例改的是头文件常量，
# 未启用 --enable-depend 时直接 make 可能复用旧对象、仍安装补丁前的产物
# 只清构建产物（make clean 删 .o/二进制等），不碰未跟踪文件；破坏性清理前先显式确认
make clean && make -j"$(nproc)" && make install

# ④ 回退：反向补丁 + 干净重编（同样不能省——撤回头文件后直接 make 可能仍安装
# 补丁版对象，让回滚「看似成功」）
# 回退前校验补丁原件仍存在，缺失即 exit，禁止无补丁硬回退
test -f "${PATCH_DIR}/num_buffer_partitions.patch" \
  || { echo "补丁原件缺失，无法 -R 回退；先恢复补丁原件，必要时从 ${BIN_BACKUP_DIR} 恢复二进制" >&2; exit 2; }
git apply -R "${PATCH_DIR}/num_buffer_partitions.patch"
make clean && make -j"$(nproc)" && make install
# ⑤ 验证运行中的目标实例已换用对应产物：重启后核对产物身份
# （pg_config --configure / md5 / LSE 计数，见
#  ../../pgsql-performance-optimization/scripts/verify_build_opts.sh）
```

- **禁止 `git clean -xfd` 清整棵源码树**：`-x` 连被 .gitignore 忽略的未跟踪文件一并删除，
  若补丁原件、applied_*.log、本地配置或构建输入放在 `${PG_SRC}` 内会被连带删除，回退依据
  与本地数据同时丢失。要换干净构建目录只清构建产物（`make clean` / 删 obj 目录）；补丁原件
  与审计记录必须置于源码树外（默认 `${PG_INSTALL}/patches`）或已提交进 git，否则 clean 会
  毁掉回退依据；如确需清理，先用 `git status --short --ignored` 列出将删除项并显式确认。
- 编译期常量类补丁（如 `NUM_BUFFER_PARTITIONS`）：只改头文件不等于生效，
  必须**从干净构建产物整树重编**（宏进每个翻译单元；未启用 --enable-depend 时
  make 不感知头文件依赖，应用与回退两侧都要 `make clean` 后重编，并验证运行实例已
  换用对应产物，不能只看 make 是否成功）；
- 补丁文件登记三要素：目的（哪个瓶颈）、出处（哪轮分析）、可逆性（-R 后树是否
  干净）；
- 多补丁叠加时按时间序逐个 apply --check，回退按反序 -R。
