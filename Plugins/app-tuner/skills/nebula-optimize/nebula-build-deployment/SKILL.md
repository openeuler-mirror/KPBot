---
name: nebula-build-deployment
description: >
  NebulaGraph 3.8.0 在鲲鹏/ARM64（aarch64）与 openEuler 上的编译、部署、数据导入、备份恢复
  与版本切换：系统依赖与 third-party 3.3、jemalloc 5.3.1、CMake/Ninja 构建（standalone 与
  多进程分布式）、LTO/LSE 优化构建、NUMA/绑核布局、端口规划、配置模板、LDBC 数据导入、
  验收检查、全量备份/恢复、二进制滚动替换与回滚。内容来自鲲鹏 950 实测部署实录，
  已参数化脱敏。用于从零搭建、复制部署、扩容、升级或排查部署问题；
  不适用参数调优（→ nebula-performance-optimization）、压测与正确性验证
  （→ nebula-test-verification）、热点归因（→ nebula-hotspot-analysis）。
  触发词：Nebula 编译、Nebula 部署、Nebula 安装、Nebula standalone、Nebula 分布式部署、
  Nebula 数据导入、Nebula 备份恢复、Nebula 升级、third-party、jemalloc、LSE、LTO。
license: MulanPSL-2.0
compatibility: NebulaGraph 3.8.0；鲲鹏 950 / aarch64、openEuler 24.03 LTS、GCC 12.3.1 实测；x86_64 可参考但需重新适配 third-party
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: graph_database
  version: "1.0.0"
---

# NebulaGraph 编译与部署

## 简介

在鲲鹏/ARM64 上从源码构建 NebulaGraph 3.8.0，并完成 standalone 或多进程分布式部署。
本文把“装依赖 → 编 third-party/二进制 → 规划拓扑与端口 → 写配置 → 启服务 → 导数据 →
验收 → 备份/恢复 → 升级/回滚”固定成可复现流程；所有命令参数化，可直接套用。

**实测基线**：鲲鹏 950（2×96 核 / 384 逻辑 CPU / 4 NUMA / 749GB）、双 NVMe、
openEuler 24.03、GCC 12.3.1、glibc 2.38、Nebula 3.8.0（tag v3.8.0 = fa928930ab34…）。
其他机型只改变量表与 NUMA 布局，不改变流程。

## 何时触发

- 在鲲鹏/openEuler 上从零搭建或重建 Nebula 集群（standalone / 1P 分布式 / 多机）。
- 编译 3.8.0、切换优化构建（LTO/LSE/jemalloc）、替换 third-party、升级第三方库。
- 规划 NUMA 绑核、端口、数据目录、分区与副本。
- 导入 LDBC/业务数据，做备份、恢复、重置、版本切换与回滚。
- 部署后验收失败：`SHOW HOSTS` OFFLINE、端口冲突、启动崩溃、数据目录不匹配。

**不适用**：线程/缓存/WAL/compaction 调优 → `nebula-performance-optimization`；
压测口径与正确性用例 → `nebula-test-verification`；火焰图与热点归因 →
`nebula-hotspot-analysis`。

## 变量约定

执行前先填表，正文与脚本统一引用这些变量；示例值仅为文档占位，不是真实环境。

| 变量 | 含义 | 示例 |
|------|------|------|
| `${NEBULA_HOME}` | Nebula 源码/运行根目录 | `/opt/nebula/nebula-3.8.0` |
| `${SRC_ROOT}` | 源码树根（可含 bak 与 working） | `/opt/src/nebula` |
| `${THIRD_PARTY}` | third-party 安装前缀 | `/opt/vesoft/third-party/3.3` |
| `${JEMALLOC_PREFIX}` | jemalloc 安装前缀（可选） | `/opt/jemalloc-5.3.1` |
| `${BUILD_DIR}` | 构建目录 | `${NEBULA_HOME}/build` |
| `${DATA_ROOT}` | storaged/meta 数据根 | `/data/nebula` |
| `${LOG_DIR}` / `${PID_DIR}` | 日志/pid 目录 | `/data/nebula/logs`、`/data/nebula/pids` |
| `${META_PORT}` | metad 端口 | 9559 |
| `${GRAPH_PORT_n}` | 第 n 个 graphd 端口 | 9670、9671、… |
| `${GRAPH_HTTP_PORT_n}` | 第 n 个 graphd HTTP 端口 | 19669、19671、19672、…（19670 是已弃用 HTTP/2） |
| `${STORAGE_PORT_n}` | 第 n 个 storaged 基础端口 | 9779、9782、9785、…（间隔 ≥3） |
| `${STORAGE_INSTANCES}` | storaged 实例数（备份/恢复遍历） | 4 |
| `${STOP_TIMEOUT}` | 停止时等待进程退出的秒数 | 120 |
| `${NEBULA_USER}` | 初始账号（仅本地测试） | root（密码用 wrapper/交互注入，不入 argv） |
| `${CONSOLE_BIN}` | nebula-console 路径 | `${NEBULA_HOME}/bin/nebula-console` |
| `${IMPORTER_BIN}` | nebula-importer 路径 | `${NEBULA_HOME}/bin/nebula-importer` |
| `${BENCH_HOME}` | NebulaGraph-Bench/k6 目录 | `/opt/bench/NebulaGraph-Bench` |
| `${CONF_DIR}` | 替换后的 flagfile 目录 | `${NEBULA_HOME}/conf` |
| `${CSV_DIR}` | LDBC/业务 CSV 目录 | `/data/ldbc` |
| `${BACKUP_ROOT}` | 备份根目录（备份写/恢复读） | `/data/nebula/backup` |
| `${RESTORE_TS}` | 恢复时指定的备份时间戳目录名 | `20261123_101500` |
| `${BACKUP_BIN}` / `${NEW_BIN_DIR}` | 二进制备份目录 / 新二进制目录 | `/data/nebula/binbak`、`/opt/nebula-new/bin` |
| `${JEMALLOC_COMMIT}` / `${K6_PLUGIN_COMMIT}` | third-party 组件版本锚点 | 见 `references/build-and-deps.md` §3/§4（jemalloc §3、K6 插件 §4） |
| `${STORAGE_HTTP_PORT_n}` | 第 n 个 storaged HTTP 端口 | 19779、19782、…（与数据口同间隔） |
| `${META_CPUS}` / `${GRAPH_CPUS[*]}` / `${STORAGE_CPUS[*]}` 等数组 | numactl 绑核参数（按实例） | 见 `references/topology-and-config.md` §2（NUMA 与绑核规划） |

凭据只允许通过环境变量或交互式 `read -rs` 注入，不写入配置文件、脚本、命令行或报告。

## 工作流总览

| 阶段 | 关键动作 | 材料 |
|------|---------|------|
| 1. 规划 | 部署形态、进程数、NUMA/绑核、端口、磁盘、分区/副本 | 本文 §部署形态；`references/topology-and-config.md` |
| 2. 前置检查 | 架构/核数/内存/磁盘/依赖/端口/时区文件/残留进程 | `scripts/preflight_check.sh`（只读） |
| 3. 构建环境 | 系统包、third-party 3.3、jemalloc、Go/k6/importer | `references/build-and-deps.md` |
| 4. 编译 | native/优化版、standalone/分布式目标、产物校验 | `references/build-and-deps.md` §5–7 |
| 5. 配置 | metad/graphd/storaged 配置、目录、端口、日志 | `references/topology-and-config.md` §4 |
| 6. 启动 | metad → graphd → ADD HOSTS（首次）→ storaged | 本文 §启动与验收 |
| 7. 导入 | CSV 去表头、importer YAML、分区、导入后对账 | `references/data-import-and-backup.md` |
| 8. 验收 | `SHOW HOSTS`、分区数、读查询、目录/端口 | 本文 §启动与验收 |
| 9. 备份/恢复 | 停服、成对拷贝 meta+storage、删 `cluster.id`、重启 | `references/data-import-and-backup.md` §6–7 |
| 10. 升级/回滚 | 二进制替换、数据兼容、最小停机、回滚 | `references/operations-troubleshooting.md` §4 |

## 部署形态选择

| 形态 | 进程 | 适用 | 注意 |
|------|------|------|------|
| standalone | 1 个 `nebula-standalone` | 功能验证、单进程性能、快速迭代 | 三服务同进程；单队列 futex 争用更明显 |
| 1P 分布式 | metad + N×graphd + M×storaged | 单机验证多进程架构、突破单进程队列瓶颈 | 端口/CPU/内存要成组规划 |
| 多机分布式 | 三类进程分布在多机 | 生产规模、水平扩展 | 网络、时钟、副本、故障域 |

选择原则：

- 只做功能与代码正确性验证：standalone 最省事。
- 要看多 graphd/多 storaged 的扩展性：1P 分布式。
- 数据量超过单机内存/磁盘，或需要容灾：多机分布式。

## 前置检查

```bash
# 只读检查：架构、NUMA、内存、磁盘、依赖、端口、时区、残留进程
NEBULA_HOME=${NEBULA_HOME} THIRD_PARTY=${THIRD_PARTY} DATA_ROOT=${DATA_ROOT} \
  bash scripts/preflight_check.sh
```

要求：

- 存在 `FAIL` 时不得开始部署；修复后重跑至无 FAIL。
- `WARN` 项（THP、numa_balancing、ulimit、时区文件缺失等）先确认影响再继续。
- 端口检查必须按“每 storaged 连续 3 个端口”计算，不能只查 `--port`。

## 构建（详见 references/build-and-deps.md）

最小路径：

```bash
# 1) third-party 3.3（aarch64 预编译包，校验 sha256 后安装）
# 2) 可选：编译 jemalloc 5.3.1（静态 + LSE）
# 3) 配置与编译
cd "${NEBULA_HOME}"
mkdir -p "${BUILD_DIR}" && cd "${BUILD_DIR}"
cmake .. -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DNEBULA_THIRDPARTY_ROOT="${THIRD_PARTY}" \
  -DENABLE_STANDALONE_VERSION=OFF \
  -DENABLE_MEMORY_TRACKER=OFF \
  -DENABLE_TESTING=OFF -DENABLE_WERROR=OFF \
  -DNEBULA_USE_LINKER=bfd \
  -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
ninja -j"$(nproc)" nebula-metad nebula-graphd nebula-storaged
```

产物校验：

```bash
ls -lh "${BUILD_DIR}"/bin/nebula-{metad,graphd,storaged}
mkdir -p "${NEBULA_HOME}/share/resources"
cp -f "${NEBULA_HOME}/resources/date_time_zonespec.csv" "${NEBULA_HOME}/share/resources/"
```

优化构建（LTO + LSE + jemalloc 5.3.1）只在明确要对比性能时使用，且必须单独记录编译参数，
不能与基线混用。详见 `references/build-and-deps.md` §5。

## 配置要点（完整模板见 references/topology-and-config.md）

- 每个 storaged 用独立 `data_path`；多盘用逗号分隔。
- `--minloglevel=3`、`--logbufsecs=30` 降低日志 I/O；压测/生产不建议长期开 `v=0` 以上。
- `--enable_optimizer=false` 只对简单 GO 等语句有意义，复杂 MATCH 保持开启。
- `--num_worker_threads` 与实例数匹配：实例越多，单实例 worker 越少，减少单队列争用。
- `--rocksdb_block_cache` 是进程级共享值，不是每 Space 一份。
- `--wal_ttl` 在只读压测可调小；生产按恢复需求保留。
- 端口：graphd 递增；storaged 间隔 ≥3（`port-1/port/port+1` 三连）。
- `--meta_server_addrs` 所有进程一致；`--local_ip` 按实际监听地址。

## 启动与验收

启动顺序（强约束）：

```
metad → graphd×N → ADD HOSTS（仅首次）→ storaged×M
```

停止顺序相反：

```
storaged → graphd → metad
```

> 启动/停止的完整命令模板见 `references/operations-troubleshooting.md` §1。
> 必须在 `${NEBULA_HOME}` 下启动（依赖 `share/resources/date_time_zonespec.csv`）；
> 清库或恢复数据后先 `rm -f cluster.id`。

验收清单：

| 检查 | 命令 | 通过标准 |
|------|------|----------|
| 进程 | `ps -ef \| grep [n]ebula` | 进程数与规划一致，无反复重启 |
| 监听 | `ss -lntp \| grep -E '9559\|967[0-9]\|977[0-9]\|978[0-9]'` | 每个实例的 3 个端口都在（含 storaged 的 978x raft 口） |
| 主机 | `SHOW HOSTS;` | 全部 `ONLINE`，分区数符合预期 |
| Space | `SHOW SPACES;` | 与导入目标一致 |
| 读路径 | `GO 1 STEP ...`、`FETCH PROP ...` | 返回非空且属性正确 |
| 日志 | `tail -n 100 ${LOG_DIR}/*stderr.log` | 无 `SIGSEGV`、`Address already in use`、`Machine not existed` |
| 数据目录 | `du -sh ${DATA_ROOT}/storage${N}/nebula/*`（N=1..M） | 每个 storaged 实例独立目录，每个 Space 有子目录且大小合理 |

## 备份、恢复与升级

完整命令与校验清单见 `references/data-import-and-backup.md` §6–8 和
`references/operations-troubleshooting.md` §4。规则：

- `--reflink=never` 确保真实拷贝，不用 COW；否则备份可能随在线数据变化。
- meta 与 storage 必须来自同一份备份；不同备份的 Space ID 可能冲突。
- 备份目录要与 RocksDB 版本匹配：RocksDB 只向后兼容，旧二进制打不开新版本数据。
- 升级前先备份、记录二进制 md5/commit、确认数据格式兼容；升级后跑功能与性能验收。

## 安全基线

- **凭据不入库、不落 argv**：密码/token 不写入脚本、配置样例、shell history；
  进程命令行可被 `ps -ef` / `/proc/<pid>/cmdline` 读取，禁止 `-p "<密码>"` 这类写法。
  用环境变量 + 交互式 `read -rs` 注入，会话结束 `unset`。
- **SSH 主机密钥校验**：默认 `StrictHostKeyChecking=accept-new`，禁止 `no` 与
  `UserKnownHostsFile=/dev/null`；主机密钥变更先查明原因，再 `ssh-keygen -R <host>`。
- **下载物校验**：third-party、jemalloc、k6/importer 等安装前校验 sha256/GPG；
  禁止 `curl … | sh` 和 `-k` 关闭 TLS。
- **变更可回滚**：二进制替换前保留 `bin.bak.<ts>`，记录 md5；配置变更保留原文件；
  失败时按停机顺序回滚并复验。
- **最小停机**：滚动替换按“停一个、换一个、验一个”的方式，避免整集群同时不可用。

## 错误处理

| 症状 | 处置 |
|------|------|
| `Invalid timezone file`（退出码 1） | 在 `${NEBULA_HOME}` 下启动，并确保 `share/resources/date_time_zonespec.csv` 存在 |
| 启动即 SIGSEGV/SIGBUS | F14/jemalloc/`-march` 与 third-party 不匹配：核对二进制 md5 与编译参数 |
| `Address already in use` | storaged 每实例占 `port-1/port/port+1`；多实例间隔不足 3，重排端口 |
| storaged `Machine not existed!` | 机器未在 metad 注册：重新执行 `ADD HOSTS`（与 `cluster.id` 无关） |
| `Reject wrong cluster host` | 清库或恢复后 `cluster.id` 残留；`rm -f cluster.id` 后重启 |
| `SHOW HOSTS` OFFLINE | 检查启动顺序、`meta_server_addrs`、storage 端口、`cluster.id`、日志 |
| 分区数不对 | 检查建 Space 的 `partition_num`、`ADD HOSTS` 是否完成、meta/storage 是否成对恢复 |
| 导入报 CSV 解析错误 | LDBC CSV 含 header；先 `sed -i '1d'` 或使用 importer `withHeader` 的正确版本 |
| 二进制替换后 `Text file busy` | 先停进程再覆盖；不要对运行中的二进制 `cp` |
| 恢复后部分 Space 丢失/ID 错乱 | meta 与 storage 不是同一份备份；重新成对恢复 |
| 编译链接 `cannot find -lstdc++` | 安装 `libstdc++-static` |
| folly F14 链接错误 | 预编译 folly 的 CRC/F14 模式与 `-march` 不一致；不要随意加 `+crc`，见 build 参考 |

## 输出契约

- **新部署/重建**：变量表、部署形态、构建参数、端口/CPU/磁盘布局、验收清单结果、
  二进制 md5 与 commit。
- **导入**：数据集版本、行数、分区/副本、导入耗时、失败数、对账结果。
- **备份/恢复**：备份路径、meta/storage 是否成对、恢复后 `SHOW HOSTS` 与 Space 清单。
- **升级/回滚**：改前/改后版本与 md5、停机窗口、验收结果、回滚命令与复验结果。
- **任何变更**：改前 → 改后、验证命令、回滚方式、风险与用户确认记录。

## 参考资料与脚本

| 文件 | 何时读 |
|------|--------|
| `references/build-and-deps.md` | 装依赖、编 third-party/jemalloc、native/优化构建、升级第三方库、编译报错 |
| `references/topology-and-config.md` | 规划 standalone/分布式、NUMA/绑核、端口、完整配置模板与参数说明 |
| `references/data-import-and-backup.md` | 准备 CSV、写 importer YAML、导入对账、备份/恢复/重置、Space ID 冲突 |
| `references/operations-troubleshooting.md` | 启停、状态、日志、常见故障、版本切换、升级回滚 |
| `scripts/preflight_check.sh` | 部署前只读检查（PASS/WARN/FAIL，有 FAIL 退出非零） |
