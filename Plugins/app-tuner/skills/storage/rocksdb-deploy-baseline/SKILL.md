---
name: rocksdb-deploy-baseline
description: 部署 RocksDB 单实例基线测试所需的依赖和构建产物。Use this skill whenever the user needs to install the RocksDB/YCSB toolchain, build RocksDB 6.26.1, or produce a runnable YCSB distribution for the RocksDB baseline workflow, even if they do not say "deploy" explicitly.
license: MulanPSL-2.0
compatibility: Requires bash, root, openEuler aarch64, numactl, Java 8, Maven, gcc 12.3.1, and either network access or offline RocksDB/YCSB sources.
metadata:
  skill_type: benchmark
  category: deployment
  domain: rocksdb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# RocksDB Baseline Deploy

准备 RocksDB 单实例基线测试所需的依赖、RocksDB jar 和可运行的 YCSB distribution。这个 skill 不做 load、warmup 或 benchmark。

## 适用场景与产物

适用于需要复用同一套 RocksDB/YCSB 构建产物执行基线测试，或后续优化测试只替换部署产物的场景。

必需输入:

- 可访问的 RocksDB/YCSB 源码或网络
- root 权限
- `WORK_ROOT` 和 `RESULT_DIR` 可写

预期产物:

```text
$WORK_ROOT/artifacts/YCSB_DIST/
  bin/ycsb
  lib/*.jar
  workloads/
$RESULT_DIR/env/deploy.env
```

`deploy.env` 记录 `YCSB_BIN`、`YCSB_WORKLOADS`、`YCSB_OPTIONS`、`YCSB_DIST`、`ROCKSDB_JAR`、`ROCKSDB_VERSION`、`ROCKSDB_FLAGS`、`JAVA_VERSION`、`GCC_VERSION`、`MAVEN_VERSION`。test skill 只消费该文件。

## 环境

- openEuler 24.03 LTS SP3, aarch64
- Java 8、gcc 12.3.1、Maven、numactl、wget、tar
- 可访问 GitHub、Maven 仓库和 openEuler yum 源；离线时需预装完整工具链，并通过 `--rocksdb-src`、`--ycsb-src`、`MAVEN_SETTINGS` 提供源码和依赖仓库
- 系统 gcc 不是 12.3.1 时，必须同时提供 `GCC_ARCHIVE_SHA256`；脚本会校验下载包摘要后再解压
- `WORK_ROOT` 至少预留 1GB；YCSB 构建和 distribution 解压需要临时空间

## 配置

```bash
bash scripts/run_deploy.sh \
  --work-root /data/rocksdb-baseline \
  --result-dir /data/rocksdb-baseline/deploy/$(date +%Y%m%d-%H%M%S)
```

可选参数:

```bash
--skip-deps
--rocksdb-src DIR
--ycsb-src DIR
--maven-settings FILE
--dry-run
```

默认 RocksDB 6.26.1、YCSB commit `19e885f7cb780fdded0547853f7810a150554caf`、Java 8、gcc 12.3.1。YCSB 使用 `assets/config/rocksdb-default.ini`。

默认 Maven 仓库为 `$WORK_ROOT/m2/repository`，会通过 `-Dmaven.repo.local` 显式传给 Maven；`--skip-deps` 默认要求 `git`、`make`、`gcc`、`g++`、`java`、`javac`、`mvn`、`tar` 已安装。

## 权限

- 安装依赖和写入系统路径需要 root；脚本在非 root 时明确失败
- 构建产物写入 `WORK_ROOT/artifacts`，源码写入 `WORK_ROOT/src`
- 不读取或输出凭据；不在镜像或日志中保存 token
- 只修改 `WORK_ROOT` 和 `RESULT_DIR` 下的产物

## 部署

```bash
bash scripts/run_deploy.sh --work-root /data/rocksdb-baseline
```

构建流程:

```text
clone 到本次部署的 $WORK_ROOT/src/YCSB-<run_id>
→ checkout 19e885f7
→ mvn -Dmaven.repo.local=$WORK_ROOT/m2/repository      -Drocksdb.version=6.26.1      -pl site.ycsb:rocksdb-binding -am clean package -DskipTests
→ 解压 rocksdb/target/ycsb-rocksdb-binding-*.tar.gz 到 staging 目录
→ 校验 bin/ycsb、rocksdbjni-$ROCKSDB_VERSION.jar 和 workloads
→ 原子切换到 YCSB_DIST
```

已存在的 `YCSB_WORK` 若含本地修改，脚本停止，不执行 `reset --hard` 或 `git clean`。

每个阶段的输出写入 `$RESULT_DIR/logs/<phase>.log`。

## 验收

部署成功后检查:

```bash
test -x "$WORK_ROOT/artifacts/YCSB_DIST/bin/ycsb"
test -f "$RESULT_DIR/env/deploy.env"
grep -q '^DEPLOY_STATUS=ok$' "$RESULT_DIR/env/deploy.env"
ls "$RESULT_DIR/env/deploy_state_"*.env
ls "$RESULT_DIR/logs"/*.log
```

`deploy.env` 中的 `YCSB_BIN`、`YCSB_WORKLOADS`、`YCSB_OPTIONS` 必须存在；`ROCKSDB_VERSION` 应为 6.26.1。失败时 `deploy.env` 写 `DEPLOY_STATUS=failed`，并记录 `FAILED_PHASE`、`FAILED_EXIT_CODE`、`FAILED_LOG`。

## 重复执行

- 每次运行使用独立的 `YCSB_WORK` 和 `deploy_state_<run_id>.env`
- 已存在 YCSB distribution 时，先由 staging 目录校验，再备份旧目录并原子切换
- 旧 `deploy.env` 会重命名为 `deploy.env.previous.<run_id>`，避免旧 `DEPLOY_STATUS=ok` 被误用
- 已存在数据目录不会在本 skill 中处理；test skill 负责测试数据的备份
- 不自动删除旧 distribution、日志或备份

## 恢复

- 构建失败时保留 `$RESULT_DIR/logs`、`deploy_state_<run_id>.env` 和已有 `YCSB_DIST`；旧 distribution 在 `.backup.<timestamp>` 目录
- staging 失败会保留 staging 目录用于检查，不覆盖正式目录
- 不自动删除源码、jar、distribution 或数据；由用户确认后手动清理
- 依赖下载失败时修复网络或提供离线源码/本地 Maven 仓库，再重新执行

## 受阻时交回

无法安装依赖、无法访问源码、磁盘不足或构建失败时，返回失败阶段、日志路径、已生成的产物和缺失项，等待人工处理。

## 参数

```bash
--work-root DIR
--result-dir DIR
--skip-deps
--rocksdb-src DIR
--ycsb-src DIR
--maven-settings FILE
--dry-run
```

## 验证

部署完成后检查 `deploy.env` 和 `YCSB_DIST/bin/ycsb` 是否存在。开发期测试脚本不随 skill 提交。
