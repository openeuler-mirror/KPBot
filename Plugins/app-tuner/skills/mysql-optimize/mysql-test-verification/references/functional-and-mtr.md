# 功能验证与 MTR

## 1. MTR 组成与原理

- `mysql-test-run.pl`：Perl 编排器，选例、启停 server、调用 mysqltest、收集结果。
- `mysqltest`：C++ 引擎（`client/mysqltest.cc`），解析 test 文件，发 SQL，比较输出。
- `mysql-test-run` / `mtr`：指向 `mysql-test-run.pl` 的软链接。
- 用例：`t/<name>.test`；期望：`r/<name>.result`；suite 在 `suite/<name>/`。
- 失败：输出与 result 不一致 → `*.reject`；server 未预期错误/崩溃/告警/超时/语法错误。

## 2. 运行方式

```
cd "${MYSQL_SRC}/mysql-test"

# 全量：mtr 失败不能被 tee 的成功退出码掩盖
set -o pipefail
./mtr --force --timestamp --max-test-fail=0 --suite-timeout=6000 \
  --testcase-timeout=300 --retry=3 --report-unstable-tests \
  --parallel=8 --big-test --mem 2>&1 | tee mtr.log
mtr_rc=${PIPESTATUS[0]}
if [ "${mtr_rc}" -ne 0 ]; then echo "mtr failed: ${mtr_rc}" >&2; exit "${mtr_rc}"; fi

# 指定
./mtr main.innodb_mysql
./mtr test1 test2
./mtr --suite=main
./mtr --suite=main,innodb
./mtr --do-test-list=collections/my.list

# 只启动实例
./mtr --start-and-exit
./mtr --start-dirty          # 复用 --start 的 datadir
```

- 必须用非 root 用户运行；mtr 会创建/删除 `var/` 数据目录。
- `--mem` 把 var 放到内存盘；需要足够内存。
- `--big-test` 才会跑标记 big 的用例（`include/big_test.inc`）。
- debug 构建才有完整 debug sync/断言；release 构建部分用例会 skip。

## 3. 常用参数

| 参数 | 说明 |
| --- | --- |
| `--record` | 重新生成 result；只应在确认语义正确后使用 |
| `--timer` | 显示用例耗时（默认开，毫秒） |
| `--warnings/--nowarnings` | 是否把 server 告警当失败（默认检查） |
| `--skip-test-list=file` | 跳过清单（放 `collections/`） |
| `--combination=...` | 同一用例用不同参数跑多遍 |
| `--debug/--debug-common` | 输出 client/server trace |
| `--manual-gdb` | 在失败点进入 gdb |
| `--force-restart` | 每个用例重启 server |
| `--repeat=N` | 重复执行 |
| `--mysqld=--opt` | 给 server 传启动参数（每个参数都要 `--mysqld=`） |
| `--strace-server` | 记录系统调用 |
| `--nocheck-testcases` | result 缺失时的判定策略 |

## 4. 用例语法要点

- Command（`--` 开头）与 SQL Statement（`;` 结尾）区分；不认识的关键字会发给 server。
- 变量：`--let $a=1`、`--inc/--dec`、`--expr $x=$a+$b`（操作数必须是变量）。
- 控制：只有 `while` 与 `if`（无 else/for）；`end` 跳出。
- 输出控制：`--echo`、`--disable_query_log`、`--disable_result_log`、
`--sorted_result`、`--replace_column`、`--replace_regex`、`--replace_result`。
- 预期错误：`--error 1050` 或 `--error ER_TABLE_EXISTS_ERROR`（推荐符号）。
- 连接：`connect (name, host, user, password, db)`、`connection name`、`disconnect name`。
- 文件：`write_file`/`append_file`/`cat_file`/`file_exists`/`remove_file` 等。
- 包含：`--source include/xxx.inc`（公共片段）。
- 跳过：`--skip message`；退出：`--exit`（不算失败）/`--die`（失败）。
- 告警抑制：`call mtr.add_suppression("正则");`。
- 时间/随机：`--replace_column 1 SECONDS` 等稳定输出。

### debug sync（协调多线程时序，仅 debug 构建）

- 前提：只有 debug 构建带 DBUG 同步点；release 构建相关用例会 skip（见 §2）。先用
  `grep -R <同步点名> sql/` 确认同步点在源码中存在，再在用例里引用；不要对 release
  实例假设 `SET DEBUG_SYNC` 生效。
- 用例语法是 SQL（`SET DEBUG_SYNC`，8.0.25 用例见
  `mysql-test/include/explain_for_connection.inc`）：

```
SET DEBUG_SYNC = 'RESET';
SET @@SESSION.DEBUG_SYNC = '<point> SIGNAL <name> WAIT_FOR <other_name>';
# 另一连接：
SET DEBUG_SYNC = 'now WAIT_FOR <name>';
SET DEBUG_SYNC = 'after_<point> SIGNAL <other_name>';
```

- 失败定位：让两个连接在指定同步点会合，配合 `connection` 切换与 `send`/`reap`
  （或 `send_eval`/`reap`）稳定复现交错时序；结束后 `SET DEBUG_SYNC='RESET'` 清理。
  卡住时先核对同步点名和连接切换顺序，再考虑调超时。
- mtr 选项 `--debug-sync-timeout=N` 设置 server 侧 `debug_sync_timeout`（8.0.25
  `mysql-test-run.pl:1639`，默认 600，经 `--loose-debug-sync-timeout` 传给 server）；
  它只控制等待超时，不替代 §5 的栈/core 证据。

## 5. 失败定位流程

1. 找 reject 与 server 错误日志，位置取决于并发度（见 §2 的 `--parallel=8`）：
   - 串行（`--parallel=1`）：`var/log/` 下有 `*.reject`、`mysqld.1.err`。
   - `--parallel>1`：每个 worker 用独立 `var/<N>/`，`.reject` 与 `mysqld.1.err` 在
     `var/<N>/log/`（`N` 为报告里的 worker 号）；`var/log/<suite>.<test>/` 只是失败现场
     存档（`my.cnf`/数据目录副本/`<test>.log`），不要把这里当 reject 与 server 日志的第一现场。
2. 看 server 日志：按上面的并发度取 `var/log/mysqld.1.err` 或 `var/<N>/log/mysqld.1.err`。
3. 复现：
   - 直接启动 mysqld 执行语句（慢、环境可能与用例不同）；
   - `./mtr --start --mysqld=--gdb` 用相同参数启动；
   - `./mtr --manual-gdb <test>` 在失败点调试。
4. 缩小：删减 test 语句，二分定位。
5. core：失败输出会给 core 路径；`find ./var -name 'core.*'`；
`gdb ${MYSQL_HOME}/bin/mysqld core.<pid>`。
6. 超时：`pstack` 看是否卡锁；提高 `--testcase-timeout`；检查死锁。
7. release 难定位：编译 debug 版本，开 `--debug-common`。

## 6. 配置文件与扩展

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `combinations` | `suite/<suite>/` | 该 suite 每个用例多参数执行 |
| `<test>.combinations` | `t/` | 单用例多参数 |
| `my.cnf` | `suite/<suite>/` | suite 级 server 配置 |
| `<test>.cnf` | `t/` | 用例级 server 配置 |
| `<test>-master.opt` / `-slave.opt` | `t/` | 启动参数/初始化参数 |
| `<test>-master.sh` / `-slave.sh` | `t/` | 启动前脚本 |
| `include/*.inc` | `include/` | 公共片段 |

## 7. 功能冒烟清单（运行实例）

只读：

- `SELECT VERSION()`、`SELECT @@sql_mode`、`SELECT @@character_set_server`。
- `SHOW DATABASES`、`SHOW VARIABLES LIKE 'innodb_buffer_pool_size'`。
- `SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%'` 计算命中率。
- `SHOW ENGINE INNODB STATUS` 无异常（死锁/长事务可解释）。

写入（隔离库，`--write-test`）：

- `CREATE DATABASE mtr_smoke_<pid>` → `CREATE TABLE`（主键/二级索引/utf8mb4）。
- `INSERT` 多行 → `SELECT` 回读 → `UPDATE` → 事务 `ROLLBACK` 验证。
- 唯一键冲突预期错误；`AUTO_INCREMENT`；`ORDER BY`/`DISTINCT` 结果正确。
- `DROP DATABASE` 清理。

不要在业务库执行写入冒烟；不要在未确认时 `--record` 覆盖 result。

## 8. 常见 MTR 问题

| 症状 | 处置 |
| --- | --- |
| `Command "perl" failed` | 安装 perl-JSON 等缺失模块 |
| 大量 skip | release 构建/缺 debug/缺插件；看 skip 原因 |
| 结果差异来自时间/随机 | 用 replace_column/replace_regex 稳定 |
| server 告警导致失败 | `add_suppression` 或 `--nowarnings`（谨慎） |
| 端口冲突 | mtr 默认用独立端口；检查残留 mysqld |
| 磁盘满 | `var/` 很大；用 `--mem` 或清理 |
| 用例超时 | 查死锁/长等待；提高 timeout 仅用于确认 |
