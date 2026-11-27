# 部署配置与验收

## 1. install.cfg（mydeploy）逐项说明

配置文件由客户/团队提供，字段名可能略有差异；下面是本系列使用过的字段与含义。

```
## 兼容版本：4.x 填 4，2.x 填 2
compatible=4
cluster_name=${CLUSTER_NAME}
cluster_num=1
config_template=custom

## 启动方式：rslist（推荐，简单）或 configserver
useRsList=true

## 共享存储/日志服务：默认关闭
ob_startup_mode=SN
shared_storage=
enableLogService=false

## Zone 与 observer 地址（单节点 zone_num=1 且只保留 zone1）
zone_num=1
zone1=<observer_ip1>
# zone_num=3 时：zone1=<ip1> / zone2=<ip2> / zone3=<ip3>

## observer 端口（注意与社区默认可能不同）
port_base=2883
mysql_port=2882
rpc_port=2881

## obproxy
proxy=<proxy_ip>
proxy_port=2828
dev=<nic_name>

## observer 规格
cpu_count=32
memory_limit=250G
dataFileSize=2000G
log_disk_size=500G

workdir=${DATA_DIR}
clog_path=${CLOG_DIR}
data_path=${DATA_DIR}
slog_path=${DATA_DIR}
log_path=

## 租户模板
tenantNum=1
tenant_template.name_prefix=tt
tenant_template.mode=mysql
tenant_template.cpu_num=10
tenant_template.memory_size=20G
tenant_template.log_disk_size=6G
tenant_template.charset=utf8mb4
tenant_template.collate=utf8mb4_general_ci
tenant_template.iops=<数值>
tenant_template.unit_num=1
tenant_template.primary_zone=RANDOM
tenant_template.locality=F

tenant1.name=${TENANT_NAME}
tenant1.mode=mysql
tenant1.cpu_num=10
tenant1.memory_size=20G
tenant1.log_disk_size=6G
tenant1.primary_zone=RANDOM
tenant1.locality=F

## 其它
enableCgroup=false
loadSpecialConfig=false
restartCluster=true
enableCDC=false
```

必改项：

| 字段 | 为什么必须改 | 改错的现象 |
| --- | --- | --- |
| `compatible` | 4.x 必须是 4 | 2.x 兼容模式，建租户/语法异常 |
| `zone_num`/`zoneN` | 与实际节点一致 | 节点注册失败/一直 `CREATING` |
| `mysql_port`/`rpc_port`/`proxy_port` | 客户端与代理要对齐 | 连不上或连到错实例 |
| `dev` | 三台机器网卡名一致 | 节点间通信失败 |
| `cpu_count`/`memory_limit` | 与规格一致 | 跑不满或 OOM |
| `workdir`/`clog_path`/`data_path` | 与实际挂载一致 | 启动失败/数据写到错误盘 |
| `locality` | 单节点必须 `F` | 单副本环境建租户卡住 |
| `primary_zone` | 测试常固定到压测机同 Zone | 跨机 RPC 拉高延迟 |

> 该配置块只作为字段说明；实际值必须以目标环境为准，不要把示例地址/目录当真实环境。

## 2. 部署步骤

```
# 以部署账号运行
su - ${ADMIN_USER}
cd ${ADMIN_HOME}/mydeploy

# 依赖与二进制准备好后
./cluster_install.sh \
  --observer ${ADMIN_HOME}/observer \
  --obproxy  ${ADMIN_HOME}/obproxy \
  --conf     ${ADMIN_HOME}/install.cfg \
  --deps_path ${ADMIN_HOME}
```

注意：

- 脚本会用 ssh 在节点间分发；先做免密与主机密钥确认。
- 部分版本脚本使用 `ssh-rsa`；新 sshd 默认禁用时需在客户端与服务端放开（测试环境），
或改用 `update-crypto-policies --set LEGACY`。
- 脚本创建目录/文件后归属必须是部署账号；否则 observer 起不来。
- 部署完成后确认 observer 进程已起、端口已监听，再建租户/起 obproxy。

## 3. 租户与用户

### 3.1 自动（install.cfg 模板）

`tenant1.*` 会创建业务租户；`cluster_install.sh` 完成后用系统租户登录确认：

```
obclient -h <observer_ip> -P ${MYSQL_PORT} -uroot@sys -Doceanbase -A
```

### 3.2 手工（理解资源模型）

以下资源/租户 DDL 在 `root@sys` 会话执行（`obclient -h <observer_ip> -P ${MYSQL_PORT} -uroot@sys -Doceanbase -A`）：

```
-- 资源单元规格
CREATE RESOURCE UNIT u_32c250g MAX_CPU 32, MIN_CPU 10, MEMORY_SIZE '250G',
  MAX_IOPS 10000000, MIN_IOPS 10000000, LOG_DISK_SIZE '500G';

-- 资源池（决定副本分布，locality 与 Zone 对应）
CREATE RESOURCE POOL p_tenant UNIT 'u_32c250g', UNIT_NUM 1, ZONE_LIST ('z1','z2','z3');

-- 租户（MySQL 模式）
-- locality 要按资源池实际 Zone 生成，不能照抄 install.cfg 模板的 FFF
CREATE TENANT ${TENANT_NAME} RESOURCE_POOL_LIST=('p_tenant'),
  PRIMARY_ZONE='z1;z2;z3', LOCALITY='F@z1,F@z2,F@z3';
```

单节点：

```
-- 单副本：Zone 只有 z1，locality 写 F@z1（不是模板里的 F）
CREATE RESOURCE POOL p_tenant UNIT 'u_32c250g', UNIT_NUM 1, ZONE_LIST ('z1');
CREATE TENANT ${TENANT_NAME} RESOURCE_POOL_LIST=('p_tenant'), PRIMARY_ZONE='z1', LOCALITY='F@z1';
```

**建完租户后必须重连到业务租户**：原 `root@sys` 会话仍指向系统租户，继续建库会落在 `sys`，给 `'%'` 授权则把全局权限暴露给任意来源，业务租户仍未初始化。

```
-- 1) 在 sys 会话核对目标租户已就绪
SELECT TENANT_NAME, TENANT_TYPE, STATUS FROM oceanbase.DBA_OB_TENANTS
  WHERE TENANT_NAME='${TENANT_NAME}';

-- 2) 退出 sys 会话，用业务租户管理员重连（密码交互输入，不写历史）
obclient -h <observer_ip> -P ${MYSQL_PORT} -uroot@${TENANT_NAME} -p

-- 3) 重连后核对当前租户/用户，确认已落在业务租户再做后续操作
SELECT CURRENT_USER(), DATABASE();
```

业务库与用户（在**业务租户**会话中执行；只授予业务库权限并限制来源主机）：

```
CREATE DATABASE IF NOT EXISTS obtest;
GRANT ALL PRIVILEGES ON obtest.* TO 'obtest'@'<app_host>' IDENTIFIED BY '<password>';
```

> 密码用交互/环境变量注入，不要写进脚本或命令历史；`<app_host>` 填实际业务来源主机/网段，不要用 `'%'`。

### 3.3 交易类模型的临时参数

某些业务模型（如交易创建）需要临时提高租户内存与冻结阈值。**变更前先记录实际值**（不要假设是 10 CPU/20G 或 20/0，否则会把原 32 CPU/250G 规格缩小）：

```
-- 记录 Unit 实际规格（两步核对，避免视图间 join 键名错配；变更前/回滚前各记一次）：
-- ① Unit 实例（归属租户/状态）
SELECT UNIT_ID, TENANT_ID, STATUS FROM oceanbase.DBA_OB_UNITS;
-- ② 资源规格（4.x 视图名为 DBA_OB_UNIT_CONFIG，单数；用 SELECT * 免列名错配，
--    列名可 SHOW CREATE VIEW oceanbase.DBA_OB_UNIT_CONFIG 复核）
SELECT * FROM oceanbase.DBA_OB_UNIT_CONFIG;
-- 记录两项内存参数实际值
SHOW PARAMETERS LIKE 'freeze_trigger_percentage';
SHOW PARAMETERS LIKE 'memstore_limit_percentage';
```

按记录值调整（示例把 Unit 内存提到 150G）：

```
ALTER SYSTEM SET freeze_trigger_percentage=30;
ALTER SYSTEM SET memstore_limit_percentage=50;
ALTER RESOURCE UNIT <unit_name> MIN_CPU=10, MAX_CPU=10, MEMORY_SIZE='150G',
  MAX_IOPS=10000000, MIN_IOPS=10000000;
```

测试结束**按变更前记录的实际值恢复**（不可写死 10 CPU/20G、20/0）：

```
ALTER SYSTEM SET freeze_trigger_percentage=<记录值>;
ALTER SYSTEM SET memstore_limit_percentage=<记录值>;
ALTER RESOURCE UNIT <unit_name> MIN_CPU=<记录值>, MAX_CPU=<记录值>,
  MEMORY_SIZE='<记录值>', MAX_IOPS=<记录值>, MIN_IOPS=<记录值>;
```

恢复后回读，与变更前记录逐项比对确认一致：

```
SELECT UNIT_ID, TENANT_ID, STATUS FROM oceanbase.DBA_OB_UNITS;
SELECT * FROM oceanbase.DBA_OB_UNIT_CONFIG;
SHOW PARAMETERS LIKE 'freeze_trigger_percentage';
SHOW PARAMETERS LIKE 'memstore_limit_percentage';
```

部分变更需要重启集群/租户才能完全生效；恢复后要重新验收。

## 4. obproxy

标准启动（示例，参数名以实际版本为准）：

```
numactl -C ${PROXY_CPUS} -m ${PROXY_NUMA} \
  ${OB_HOME}/proxy/bin/obproxy \
  -p ${PROXY_PORT} -c ${CLUSTER_NAME}.admin \
  -r <observer_ip>:${MYSQL_PORT} \
  -o with_config_server=false,enable_cluster_checkout=false,enable_strict_kernel_release=false
```

注意：

- `-r`（OBServer 列表）要传 observer 的 **SQL 端口（MySQL 端口 `${MYSQL_PORT}`）**，不是 RPC 端口 `${RPC_PORT}`；填错代理无法建立预期的 SQL 连接。
- 凭据/系统密码用安全方式注入，不要出现在 argv。
- obproxy 与 observer 尽量同 NUMA/P；obproxy 的 `work_thread_num` 要与连接数匹配。
- 客户端连接串：`obclient -h <proxy_ip> -P ${PROXY_PORT} -u<user>@<tenant>#<cluster> -p`。
- 经代理查询业务租户验收（确认代理走 SQL 端口、业务租户可查；密码用 `-p` 交互输入）：

```
obclient -h <proxy_ip> -P ${PROXY_PORT} -u"root@${TENANT_NAME}#${CLUSTER_NAME}" -p -e \
  'SELECT CURRENT_USER(), DATABASE(), VERSION(); SHOW DATABASES;'
```

## 5. 启停顺序

启动：

```
observer（全部节点，等待注册/ACTIVE）
  → 系统租户检查 DBA_OB_SERVERS/ZONES
  → 业务租户与用户可用
  → obproxy
  → 压测/业务
```

停止：

```
停止压测/业务
  → obproxy
  → observer（先备后主，或按运维平台顺序）
```

单机三副本：先停 2 个备副本，再停主；启动反之（先起 2 个，再起主）。

## 6. 验收 SQL（只读）

```
-- 节点（4.3.5 的 DBA_OB_SERVERS 用 SVR_PORT/SQL_PORT，没有 RPC_PORT 列；
--       上线前先在目标版本用 DESC 核对列名）
-- DESC oceanbase.DBA_OB_SERVERS;
SELECT SVR_IP, SVR_PORT, SQL_PORT, WITH_ROOTSERVER, STATUS, BUILD_VERSION
  FROM oceanbase.DBA_OB_SERVERS;

-- Zone
SELECT * FROM oceanbase.DBA_OB_ZONES;

-- 租户
SELECT TENANT_ID, TENANT_NAME, TENANT_TYPE, PRIMARY_ZONE, LOCALITY, COMPATIBILITY_MODE, STATUS
  FROM oceanbase.DBA_OB_TENANTS;

-- Unit 规格
-- 两步核对（join 键在不同 4.x 小版本有差异，分开查最稳）：
SELECT UNIT_ID, TENANT_ID, STATUS FROM oceanbase.DBA_OB_UNITS;
SELECT * FROM oceanbase.DBA_OB_UNIT_CONFIG;

-- 参数
SHOW PARAMETERS LIKE '%port%';
SHOW PARAMETERS LIKE '%memory%';
SHOW PARAMETERS LIKE '%cpu%';
```

业务库验收：

```
SHOW DATABASES;
USE obtest;
SHOW TABLES;
SELECT COUNT(*) FROM <table>;
SELECT * FROM <table> LIMIT 5;
```

写回读（隔离库）：

```
-- 本轮唯一表名：追加时间戳/随机后缀（如 t_smoke_20241126_001），每轮换新名，
-- 先确认该表名不存在；若已存在则换名，不要继续。
SHOW TABLES LIKE 't_smoke_20241126_001';
-- CREATE 若报错（已存在/无权限/只读），立即停止，不要继续粘贴后续 INSERT/DELETE/DROP，
-- 否则会删行并 DROP 掉本轮并未创建的原表。
CREATE TABLE t_smoke_20241126_001(id INT PRIMARY KEY, v VARCHAR(32));
INSERT INTO t_smoke_20241126_001 VALUES (1,'ok');
SELECT * FROM t_smoke_20241126_001 WHERE id=1;
DELETE FROM t_smoke_20241126_001 WHERE id=1;
-- 只清理本轮成功创建的那张表
DROP TABLE t_smoke_20241126_001;
```

## 7. 验收通过标准

| 项 | 标准 |
| --- | --- |
| 进程 | observer/obproxy 数量与规划一致，运行 5 分钟无重启 |
| 端口 | mysql/rpc/proxy 均监听，外部可连 |
| 节点 | `DBA_OB_SERVERS` 全 `ACTIVE`，`BUILD_VERSION` 与二进制一致 |
| Zone | 数量与 `zone_num` 一致，`STATUS` 正常 |
| 租户 | 业务租户 `STATUS=NORMAL`，`LOCALITY`/`PRIMARY_ZONE` 符合设计 |
| Unit | CPU/内存/IOPS 与规格一致 |
| 读写 | 计数/抽样/写回读全部通过 |
| 日志 | 无 `ERROR`/`disk full`/`paxos` 异常/`core` |
| 目录 | data/clog 分盘，权限正确，空间充足 |
| 绑核 | `numactl`/cgroup 生效；观察无大量跨 NUMA 访存 |

## 8. 常见部署失败

| 症状 | 优先检查 |
| --- | --- |
| 租户一直 `CREATING` | `locality` 是否单副本、Zone/Unit 是否够、observer 是否 ACTIVE |
| `DBA_OB_SERVERS` 为空 | observer 未注册、RPC 端口不通、`cluster_name` 不一致 |
| obclient 连接报租户不存在 | `user@tenant` 写法、端口（MySQL vs obproxy）、集群名后缀 |
| 建库/写入报只读 | 租户/副本未就绪、磁盘满、Unit 资源不足 |
| observer 启动即退出 | 目录权限、clog 空间、内存不足、`datafile_size` 超过盘 |
| 启动后 insert 明显慢 | 启动时绑核/绑内存是否生效（跨 P） |
