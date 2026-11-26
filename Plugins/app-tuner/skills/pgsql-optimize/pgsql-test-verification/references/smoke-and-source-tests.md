# 冒烟与源码 ST/UT 测试

> 冒烟全部只读（SELECT）；源码测试自起临时实例（src/test/regress 临时安装），
  不碰 ${PGDATA} 生产实例。

## 1. 只读 SQL 冒烟（scripts/smoke_test.sh 的口径）

分节独立判定（与主批次同级的独立校验，任一节 FAIL 即非零退出）：

| 节 | 断言 | 说明 |
|----|------|------|
| 版本 | `SELECT version()` 含期望版本串（如 `17.5`） | 与 pg_config --version 同口径 |
| 扩展 | `SELECT extname, extversion FROM pg_extension` 含预期扩展（pg_stat_statements 等） | 缺扩展=部署不完整 |
| 数据 | `SELECT count(*) FROM <表>` 与导入对账值一致 | 每次独立 psql 会话（psql -c 每次新连接） |
| 一致性 | `SELECT 1`、`SELECT current_setting('shared_buffers')` 可读 | 恢复中/只读模式会在此暴露 |
| 写可用性（写负载必做） | `SELECT pg_is_in_recovery()`=f 且 `SHOW transaction_read_only`=off | 可读热备也能过前几项——写压测连错备库时在这里拦截 |
| 等待事件 | `SELECT wait_event_type, wait_event, state FROM pg_stat_activity WHERE state='active'` 无长期异常等待（Lock > 阈值样本） | 空转时采不到属正常 |

- psql 每次调用都是新会话：脚本内多段检查不共享会话状态（同库无需 USE 概念，
  但 `-d ${DBNAME}` 每次都要带——漏了会落到默认库误判失败）。
- 退出码：0=全 PASS；1=存在 FAIL；2=用法/连接失败。

## 2. 源码 ST/UT（make check 家族）

```bash
cd "${PG_SRC}"
# 回归测试（自起临时实例：src/test/regress/ 下 tmp_check/，不动 ${PGDATA}）
make check                     # 核心 + contrib 已装部分
make check-world               # 全量（含 contrib/isolation/recovery/安全测试），耗时按小时计
```

- `make check` 的机制：先 `temp-install`（临时安装到 src/test 下），再按
  `src/test/regress/parallel_schedule` 跑 SQL 对 `.expected/out` diff——
  **failures 视为回归**（个别环境差异项在 `expected/*.source` 生成时已吸收）；
- 变更涉及 planner/executor 时至少跑核心回归 + `src/test/isolation`（并发可见
  死锁/锁序问题）；涉及 WAL/恢复跑 `src/test/recovery`。

## 3. TAP 测试（t/*.pl）

```bash
# 单套件（以 recovery 为例）
make -C src/test/recovery check
# isolation
make -C src/test/isolation check
# 贡献模块内 t/ 目录随 make -C contrib/<name> check 跑
```

- TAP 套件位置：`src/test/<suite>/t/*.pl`（Perl + PostgreSQL::Test::Cluster）；
  **不是** `src/test/regress/t/`——regress 是 SQL 对照，没有 t/ 子目录（常见
  找错位置的坑：按目录名搜 t/ 会漏掉 regression 的 SQL 模型）。
- `prove` 可直接驱动：`prove -I src/test/perl/ src/test/recovery/t/001*.pl`
  （需要源码树的 perl 模块路径）；CI 侧统一走 make 包装。
- **门禁有效性核查**：configure 未加 `--enable-tap-tests` 时，recovery 的
  `make check` 可能在提示 TAP 未启用后**成功退出**——零个恢复测试执行也算“通过”。
  WAL/恢复类变更的门禁必须：①核对 `pg_config --configure` 含 `--enable-tap-tests`；
  ②确认 perl IPC::Run 已安装；③在输出日志中数出实际执行且通过的测试数（>0），
  三个条件齐了才算门禁通过。

## 4. 补丁后的最低测试矩阵

| 变更类型 | 最低测试 |
|---------|---------|
| 头文件常量（NUM_BUFFER_PARTITIONS 等） | make check + 冒烟 + 三负载 A/B |
| LWLock/锁路径 | make check + isolation 全量 |
| WAL/提交路径 | make check + recovery 全量 |
| contrib/自研扩展 | make -C contrib/<name> check + 扩展视图冒烟 |
