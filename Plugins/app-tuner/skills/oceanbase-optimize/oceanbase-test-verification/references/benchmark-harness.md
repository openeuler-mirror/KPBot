# 压测工具链与场景

## 1. sysbench 安装（1.0.20 + LuaJIT 2.1）

固定版本 + 冻结摘要：两腿 A/B 必须来自同一份归档；禁止可变分支头、禁止关闭 TLS
校验（`--no-check-certificate` / `-k` 会允许中间人替换源码并在压测机上以当前用户编译执行）。

```
# 依赖：先安装/更新 CA 证书，全程启用 TLS 校验
yum -y install ca-certificates autoconf libtool mysql mysql-devel vim unzip

SYSBENCH_TAG=1.0.20
LUAJIT_TAG=v2.1.0-beta3            # 固定 tag，不用 refs/heads/v2.1（分支头可变、可被 force-push）
SYSBENCH_ZIP="sysbench-${SYSBENCH_TAG}.zip"
LUAJIT_TAR="LuaJIT-${LUAJIT_TAG#v}.tar.gz"
LUAJIT_DIR="LuaJIT-${LUAJIT_TAG#v}"

wget -O "${SYSBENCH_ZIP}" "https://codeload.github.com/akopytov/sysbench/zip/refs/tags/${SYSBENCH_TAG}"
wget -O "${LUAJIT_TAR}" "https://github.com/LuaJIT/LuaJIT/archive/refs/tags/${LUAJIT_TAG}.tar.gz"

# 摘要须由维护者从官方 release/签名冻结后下发（不能"下载后现算"回填期望值）。
# 缺摘要或摘要不符立即中止，不得编译、更不得把不同归档的编译产物放进同一组 A/B。
: "${SYSBENCH_SHA256:?请填入官方冻结的 sysbench 1.0.20 归档 sha256}"
: "${LUAJIT_SHA256:?请填入官方冻结的 LuaJIT ${LUAJIT_TAG} 归档 sha256}"
printf '%s  %s\n%s  %s\n' \
  "${SYSBENCH_SHA256}" "${SYSBENCH_ZIP}" \
  "${LUAJIT_SHA256}" "${LUAJIT_TAR}" > SHA256SUMS.expected
sha256sum -c SHA256SUMS.expected || {
  echo "FAIL 归档摘要不符：中止编译（上报维护者，禁止替换期望值绕过）" >&2
  exit 1
}

unzip -q "${SYSBENCH_ZIP}"
tar -xzf "${LUAJIT_TAR}"

# 替换随包的 luajit（目录名随固定 tag，不随分支漂移）
mv sysbench-1.0.20/third_party/luajit/luajit sysbench-1.0.20/third_party/luajit/luajit-bak
cp -r "${LUAJIT_DIR}" sysbench-1.0.20/third_party/luajit/luajit

# 提高线程初始化超时（高线程数/慢环境必需）
#   src/sysbench.c: #define THREAD_INIT_TIMEOUT 300
cd sysbench-1.0.20
./autogen.sh && ./configure && make -j"$(nproc)" && make install
sysbench --version
```

> 归档清单必须记录：两个归档的 sha256、LuaJIT 固定 tag（`v2.1.0-beta3`）与其冻结时
> 对应的 commit（`git ls-remote https://github.com/LuaJIT/LuaJIT "${LUAJIT_TAG}"`）、
> 解压后版本串、sysbench/luajit 二进制 md5 与编译参数，口径见 `ab-protocol.md` §归档。
> 缺任一项时本轮数据标为不可比，不得宣称 A/B 收益。

客户端文件描述符：

```
# 取 min(需求, /proc/sys/fs/nr_open)；默认内核 fs.nr_open=1048576，设成 10^9 会 EPERM
ulimit -SHn 1000000
```

## 2. 五场景

| 场景 | lua | 说明 |
| --- | --- | --- |
| write_only | `src/lua/oltp_write_only.lua` | 纯写（INSERT/UPDATE/DELETE 混合） |
| read_only | `src/lua/oltp_read_only.lua` | 混合读（含范围/排序/聚合） |
| point_select | `src/lua/oltp_point_select.lua`（支持 `--tables`，多表/扩展性用） | 随机点查 |
| insert | `src/lua/oltp_insert.lua` | 单表插入 |
| update | `src/lua/oltp_update_non_index.lua` | 非索引列更新（update_non_index） |

> 点查脚本二选一：`select_random_points.lua` 在 `prepare()`/`cleanup()` 里硬断言
> `tables == 1`、查询硬编码 `FROM sbtest1`，只能 `--tables=1`；多表/30 表扩展性专项
> 必须用 sysbench 自带的 `oltp_point_select.lua`（上表默认），否则 prepare/cleanup 直接断言失败。

统一参数（实测基线）：

```
THREADS=800
TIME=300
TABLES=1
TABLE_SIZE=500000
--db-ps-mode=disable
--mysql-ignore-errors=1062,6002,6001,4012,1213,1020,1205,4038,6004
--rand-type=uniform
--report-interval=1
--percentile=99
```

> `--mysql-ignore-errors` 在压测中用于跳过可预期冲突/超时；报告里必须说明，否则
> “成功率高”可能是忽略了错误。

## 3. 连接

口令一律经 `--config-file` 传入（生成方式见第 4 节）：不要写
`--mysql-password=<...>`，未加引号的尖括号会被 Bash 当成重定向。

### 3.1 经 obproxy（业务形态）

```
sysbench --config-file="${SYSBENCH_CONF}" --mysql-host="${PROXY_IP}" \
  --mysql-port=${PROXY_PORT} --mysql-user='obtest@mysql_tenant#obcluster' \
  --mysql-db=obtest ...
```

### 3.2 直连 observer（对照）

```
sysbench --config-file="${SYSBENCH_CONF}" --mysql-host="${OBSERVER_IP}" \
  --mysql-port=${MYSQL_PORT} --mysql-user='obtest@mysql_tenant' \
  --mysql-db=obtest ...
```

两者是不同口径：obproxy 有转发/协议/队列开销，直连少一跳。比较优化收益时两腿必须一致。

## 4. 标准压测脚本骨架

```
#!/bin/bash
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/lib64/mysql:.
USER='obtest@mysql_tenant#obcluster'
HOST='<proxy_ip>'          # 执行前替换为实际地址
PORT=${PROXY_PORT}
PASSWORD='<password>'      # 由环境/交互输入，不硬编码进仓库
DB=obtest
TABLES=10
TABLE_SIZE=500000
THREADS=800
TIME=300
LUA=oltp_write_only.lua
PATH_LUA=/usr/local/share/sysbench/$LUA

# 凭据走 sysbench 1.0.20 支持的 --config-file：key=value、无 [section]；
# 0600 临时文件 + trap 清理，三个阶段共用同一份配置，口令不进 argv/日志
umask 077
SYSBENCH_CONF="$(mktemp)"
RUN_PID=""
cleanup() {
  # nohup 的 run 可能还在启动/运行中，等它读完配置再删，避免竞态
  if [ -n "${RUN_PID}" ]; then wait "${RUN_PID}" 2>/dev/null || true; fi
  rm -f "${SYSBENCH_CONF}"
}
trap cleanup EXIT INT TERM
{
  printf 'mysql-host=%s\n' "${HOST}"
  printf 'mysql-port=%s\n' "${PORT}"
  printf 'mysql-user=%s\n' "${USER}"
  printf 'mysql-password=%s\n' "${PASSWORD}"
  printf 'mysql-db=%s\n' "${DB}"
} > "${SYSBENCH_CONF}"

# 清理 + 准备（三阶段统一传 --tables，实际表数与报告口径一致）
sysbench --config-file="${SYSBENCH_CONF}" "${PATH_LUA}" --db-ps-mode=disable \
  --tables="${TABLES}" --threads="${THREADS}" --table_size="${TABLE_SIZE}" cleanup

sysbench --config-file="${SYSBENCH_CONF}" "${PATH_LUA}" --db-ps-mode=disable \
  --tables="${TABLES}" --threads="${THREADS}" --table_size="${TABLE_SIZE}" prepare

# 运行
DATE=$(date +%Y%m%d-%H%M%S)
nohup sysbench --config-file="${SYSBENCH_CONF}" "${PATH_LUA}" --db-ps-mode=disable \
  --report-interval=1 --mysql-ignore-errors=1062,6002,6001,4012,1213,1020,1205,4038,6004 \
  --tables="${TABLES}" --threads="${THREADS}" --table_size="${TABLE_SIZE}" \
  --time="${TIME}" --max-requests=0 --rand-type=uniform \
  > "${LUA}.${DATE}.log" 2>&1 &
RUN_PID=$!
```

> 口令不要硬编码在脚本里；由环境变量/交互输入注入 `PASSWORD`，再经 `--config-file`
> 传入（sysbench 1.0.20 的 `key=value` 格式，无 `[section]`）。值以 `#` 开头或含
> 逗号/空白时按 sysbench 配置规则加引号。`--tables` 在 cleanup/prepare/run 三阶段必须
> 一致，否则实际表数与 `TABLES`/报告口径不符。

## 5. 业务模型

| 场景 | lua | 依赖脚本 | 参数/说明 |
| --- | --- | --- | --- |
| 交易创建 | `create_trade_list_pg.lua` | `obtrade_common_list_pg.lua` | 需临时调租户内存与冻结阈值 |
| 交易支付 | `pay_trade_list_pg.lua` | `obtrade_common_pay_list.lua` | — |
| 账务 | `test_acctrans.lua` | 无 | 写放大，注意 redo/clog |

> 这三个 lua 不属于 sysbench 发行版，也无公开仓库，属于业务方私有资产：缺省不可复现，
> 需业务方提供脚本后才能跑；未提供时仅 sysbench 五场景可复现，不要声称已验证业务模型。
> `_pg` 只是脚本文件的来源命名，不代表连接协议——在 OB MySQL 模式下仍经 `${MYSQL_PORT}`
> 连接，据此记录口径。

业务模型与 sysbench 标准场景不可混比：SQL 形态、事务大小、冲突率都不同。

## 6. 规格与线性度

| 规格 | 线程（实测） | 表 | 单表行数 | 说明 |
| --- | --- | --- | --- | --- |
| 32U | 800 | 1 | 50w | 本系列主基线 |
| 64U | 800（或 512/1024） | 1 | 50w/100w | 需同步放大租户资源 |
| 128U | 800–1024 | 1/30 | 100w | 点查扩展性常用 30 表 |
| 扩展性专项 | 随规格调 | 30 | 100w | 只跑 point_select（`oltp_point_select.lua`，支持 30 表；不要用仅支持 `--tables=1` 的 `select_random_points.lua`），调到 observer 打满 |

线性度（32U/16U，实测）：AMD 9T34 约 1.93 > 鲲鹏 920B 1.87 > 鲲鹏 950 1.64。
评估大规格前先测小规格与线性度，避免把“扩展性差”误判为“参数没调好”。

## 7. 客户端与服务端同时观察

| 观察 | 命令 | 判读 |
| --- | --- | --- |
| observer CPU | `pidstat -p <pid> 1`、`mpstat -P ALL 1` | 目标核是否打满；是否跨 NUMA |
| obproxy CPU | `pidstat -p <pid> 1` | 转发是否成为瓶颈 |
| sysbench CPU | `pidstat -p <pid> 1` | 客户端是否先饱和 |
| 磁盘 | `iostat -x 1` | 写场景 await/util |
| 网络 | `sar -n DEV 1`、`ss -s` | 带宽/连接/重传 |
| OB 内部 | `GV$SYSSTAT`（见 hotspot skill） | IO/RPC/memstore 指标 |

## 8. 数据重置

- 每轮压测前 `cleanup + prepare`，或从同一备份恢复；不要在上轮脏数据上继续。
- 读场景预热：先跑 30–60s 再开始计时。
- 冻结/合并会周期性影响写场景；记录压测期间是否发生冻结/合并。
- 多轮之间保持同一 `primary_zone`、同一绑定、同一 obproxy 配置。

## 9. 最小压测记录

```
OB: <version> @ <commit>, observer md5=<...>, obproxy md5=<...>
compiler: <toolchain/flags>
deploy: <single/3-replica>, spec=<32U/64U/128U>, unit=<cpu/mem>
binding: observer=<cpus/mem node>, obproxy=<...>, sysbench=<...>
tenant: <name>, primary_zone=<...>, locality=<...>
dataset: tables=<n>, table_size=<n>, prepared_at=<...>
scenario: <lua>, mode=<proxy|direct>, threads=<n>, time=<n>s, rounds=<n>
result: tps=<...>, qps=<...>, err=<...>, avg=<...>, p95=<...>, p99=<...>, cpu=<...>
```

## 10. 指标口径

| 指标 | 含义 | 注意 |
| --- | --- | --- |
| TPS | 事务吞吐 | sysbench 1.0.20 输出行为 `transactions:  N (X per sec.)`（累计段）；读场景常看 QPS |
| QPS | 语句吞吐 | 对应 `queries:  N (Y per sec.)`；中间段为 `eps:`/`lat (ms,P%):`。按实际输出原文记录，不要按不存在的 `transactions per second` 字样检索 |
| 成功率 | 失败/错误码 | 1062 等需按场景解释；忽略的错误码与比例必须记录 |
| `avg` / `95th` / `99th` | 延迟 | 均值掩盖长尾；同时看 max；整轮分位取汇总值，不用区间 p95 反推 |
| CPU% | observer/obproxy/sysbench | 目标核是否打满；是否跨 NUMA（采集命令见 §7） |
| 客户端 CPU | sysbench 进程 | 接近满载说明客户端先瓶颈 |
| 磁盘 IO | iostat/`GV$SYSSTAT` io 指标 | 写场景必看 |
| 冻结/合并 | 日志/线程 CPU | 周期性 QPS 下降的解释 |

工具链速查：sysbench 1.0.20（LuaJIT 2.1，`THREAD_INIT_TIMEOUT` 调大见 §1）、obclient 2.2.x
（连接验收，见 `functional-verification.md` §3）、obproxy 可选（直连/代理两口径不能混比，见 §3）、
客户端与服务端分核/分机、多实例时每实例一组 sysbench/obproxy。
