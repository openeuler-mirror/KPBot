# Doris 部署与 TPC-H 测试

## 环境与部署

已有集群先检查 FE/BE 状态。新部署根据 [目标版本安装文档](https://doris.apache.org/docs/3.x/gettingStarted/quick-start/) 从 [官方下载页](https://doris.apache.org/download/) 选择对应架构的 Binary 发行包（不要选 Source）并下载页面提供的 SHA-512 校验文件；旧版本从该页的 Archived Releases 获取，确认所需 JDK、内核参数、磁盘与容量；版本不同先切换文档版本。按实际拓扑设置 FE 的 priority_networks、meta_dir 与 BE 的 priority_networks、storage_root_path，安装位置由用户选择。

在解压后的发行包根目录启动 FE：`bash fe/bin/start_fe.sh --daemon`；在各 BE 节点的发行包根目录启动 BE：`bash be/bin/start_be.sh --daemon`。通过 MySQL 客户端连接 FE，先执行 SHOW BACKENDS，尚未注册的节点才执行 `ALTER SYSTEM ADD BACKEND "be-host:9050"`，be-host 与端口替换为真实节点信息。

启动前在实际 BE 宿主机执行 `sysctl -n vm.max_map_count`，3.0.8 的启动脚本要求至少 2000000。Docker 容器共享此宿主机参数，不能只在容器配置里填写。需要调整且已有对应授权时，先记录原值，再临时执行 `sysctl -w vm.max_map_count=2000000`；验证结束停止本次 BE 后恢复原值，不默认写入永久配置。FE 的 JVM 堆和 BE 的 mem_limit 还需按容器/进程实际内存预算设置，不能根据宿主机总内存直接分配。

BE 启动脚本还检查 `ulimit -n` 至少为 60000，以及 swap 状态。共享测试机不要擅自关闭宿主机 swap。容器可以设置相同的 `--memory` 与 `--memory-swap` 限额来禁止自身使用 swap；若 3.0.8 包装脚本仍因宿主机 swap 而拒绝启动，先独立核对 cgroup v2 的 memory.swap.max 为 0（v1 则 memory.memsw.limit_in_bytes 与 memory.limit_in_bytes 相等），并确认映射上限和文件限制均达标，再使用上游提供的 `SKIP_CHECK_ULIMIT=true` 环境变量启动 BE，记录这一测试条件；不得用它掩盖未满足的资源条件。

Docker 的文件句柄默认值不一定继承宿主机的大限额；创建测试容器时显式设置 `--ulimit nofile=655350:655350`，启动后在同一容器内核对 `ulimit -n`。

```bash
mysql --host localhost --port 9030 --user root --execute 'SHOW FRONTENDS; SHOW BACKENDS;'
```

确认目标 FE 的 Join/Alive 与各预期 BE 的 Alive 为真，检查磁盘与日志。容器里的服务地址、广告地址和存储挂载按实际拓扑设置。同机多容器的 CPU quota 与 cpuset 分别记录。

示例命令在 Linux/Bash 下执行。需安装 MySQL 兼容命令行客户端；若系统只提供 mariadb 命令，最小验证可替换命令并给检查脚本传 `--client mariadb`。下面 TPC-H 上游脚本直接调用 mysql，因此运行该工具链前需提供可用的 mysql 命令。

## 最小 SQL 验证

```bash
mysql --host localhost --port 9030 --user root --batch --skip-column-names --execute 'SELECT 1'
mysql --host localhost --port 9030 --user root --batch --skip-column-names --execute 'SELECT SUM(v) FROM (SELECT 1 AS v UNION ALL SELECT 2 UNION ALL SELECT 3) AS t'
```

期望分别为 `1` 与 `6`。自动检查入口为 `scripts/check_service.py`。这是连接与 SQL 计算检查；集群的数据读写能力还须通过下述建表、导入和查询验证。

## 可公开获取的 TPC-H 工具

使用 [官方 TPC-H 流程](https://doris.apache.org/docs/3.x/benchmark/tpch/) 的完整公开工具目录，不能只下载 bin 中的几个脚本。下面提供 Doris 3.0.8 的固定工具版本例子；其他服务版本使用其实际存在的 tag 并核对工具兼容性，版本号不等于 Git tag 名。

```bash
git clone --depth 1 --filter=blob:none --sparse --branch 3.0.8-rc01 https://github.com/apache/doris.git doris-source
git -C doris-source sparse-checkout set tools/tpch-tools
git -C doris-source rev-parse HEAD
cd doris-source/tools/tpch-tools
```

这个 tag 对应 commit `09b0cc49a60ffdd444df3e40e5f3dc180299b561`；目录包含 bin、conf、ddl 和 queries。运行主机需要 Bash、Git、GNU getopt/coreutils、gcc（该工具例子使用 GCC 14 或更早的兼容 C 工具链）、make、wget、unzip、curl 和 mysql。构建脚本会自动下载公开的 TPC-H 生成器；离线时将网络下载及构建放在相同目标架构的联网构建机，打包整个 tools/tpch-tools 目录转入测试机。

Git 传输不可用时，也可在 skill 根目录使用随附的标准库下载器，仅取得工具目录，避免下载整个仓库：

```bash
python3 scripts/fetch_tpch_tools.py --ref 3.0.8-rc01 --output experiment/tpch-tools
cd experiment/tpch-tools
```

下载器先将 ref 解析到 commit，再通过 GitHub 的公开 API/Raw 下载完整目录，逐文件校验 Git blob SHA-1，保存 source.json；输出目录已存在时拒绝覆盖。目标机不能访问 API/Raw 时，可在联网机器运行该命令，然后将生成目录整体复制到目标机。下面所有 bin 命令均可在这个工具目录中执行，无需另外创建脚本。

将 conf/doris-cluster.conf 中 FE_HOST、FE_QUERY_PORT、FE_HTTP_PORT、USER、DB 设置为目标实验集群；DB 使用新的隔离库名。PASSWORD 不填写明文，将该文件原有的 PASSWORD 赋值行替换为下面这一行（保留字面量变量引用）：

```bash
export PASSWORD="${DORIS_PASSWORD?set DORIS_PASSWORD explicitly}"
```

在运行下列 bin 命令的同一个 Bash 会话中，用 `read -r -s -p 'Doris password: ' DORIS_PASSWORD; printf '\n'; export DORIS_PASSWORD` 隐藏输入口令，也可由受控环境注入；空口令需要显式设置 `export DORIS_PASSWORD=''`。不要开启 shell 跟踪，不把展开后的配置或凭据写入结果文件。完成后执行 `unset DORIS_PASSWORD MYSQL_PWD`。最小检查脚本的 `--config` 认证文件应由用户在 Git 工作树之外创建、限制为仅本人可读，并通过 `--config "$DORIS_CLIENT_CONFIG"` 引用。

skill 的忽略规则覆盖 results、experiment、cases 下的 results 以及示例源码目录；它只是防误提交措施，不能代替凭据管理。先阅读建表脚本，确认只作用于该实验库。

SF=1 起步，容量验证后再调整；以下命令均在 tools/tpch-tools 目录执行，任何一步失败即停止：

```bash
bash bin/build-tpch-dbgen.sh
bash bin/gen-tpch-data.sh -s 1 -c 2
bash bin/create-tpch-tables.sh -s 1
bash bin/load-tpch-data.sh -c 1
bash bin/run-tpch-queries.sh -s 1
```

导入后比对八表 COUNT(*)、生成数据记录数和导入任务状态。检查查询结果后，先预热再多轮测量，保存每次脚本输出与查询级耗时。规模、分桶、副本、会话设置、查询集合与集群资源固定。比较参数时一次只改一个变量；内存不足先关联 BE 指标、容器限制与导入/compaction 状态，不能仅凭日志关键字改 OS 参数。

生成数据保存在 `bin/tpch-data/`；不要将已有目录复用成新一轮数据。SF=1 的 DDL 使用单副本并含 DROP TABLE/VIEW，必须使用新的实验库，其他规模的 DDL 另行核对副本数和 BE 数量。Stream Load 的 JSON 响应必须逐项检查 Status、过滤行和导入行，HTTP/脚本成功退出不等于导入成功。

该版本生成脚本始终给大表生成器传入分片参数，`-c` 至少取 2，不能使用 1。生成完成必须确认八表文件均存在；SF=1 的 region/nation/supplier/part/partsupp/customer/orders/lineitem 行数依次应为 5、25、10000、200000、800000、150000、1500000、6001215，导入后逐表比对。后台生成任务失败可能不反映在脚本退出码中，不得只凭退出码进入查询阶段。

查询脚本自带一轮首次运行与两轮热运行，报告的是客户端墙钟毫秒，不能声称每轮都清除了缓存。它在 `bin/result/` 写查询输出，在调用目录追加 `result.csv`，再次运行会清理 bin/result。每次完成后先复制这两个位置和 stdout/stderr 到新的轮次目录，再开始下一次；比较结果时保留完整查询集合，不把失败查询算作零耗时。
