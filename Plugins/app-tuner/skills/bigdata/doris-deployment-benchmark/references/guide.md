# Doris 部署与 TPC-H 测试

## 环境与部署

已有集群先检查 FE/BE 状态。新部署根据 [目标版本安装文档](https://doris.apache.org/docs/3.x/gettingStarted/quick-start/) 从 [官方下载页](https://doris.apache.org/download/) 选择对应架构的 Binary 发行包（不要选 Source）并下载页面提供的 SHA-512 校验文件；旧版本从该页的 Archived Releases 获取，确认所需 JDK、内核参数、磁盘与容量；版本不同先切换文档版本。按实际拓扑设置 FE 的 priority_networks、meta_dir 与 BE 的 priority_networks、storage_root_path，安装位置由用户选择。

在解压后的发行包根目录启动 FE：`bash fe/bin/start_fe.sh --daemon`；在各 BE 节点的发行包根目录启动 BE：`bash be/bin/start_be.sh --daemon`。通过 MySQL 客户端连接 FE，先执行 SHOW BACKENDS，尚未注册的节点才执行 `ALTER SYSTEM ADD BACKEND "be-host:9050"`，be-host 与端口替换为真实节点信息。

启动前在实际 BE 宿主机执行 `sysctl -n vm.max_map_count`，3.0.8 的启动脚本要求至少 2000000。Docker 容器共享此宿主机参数，不能只在容器配置里填写。需要调整且已有对应授权时，先记录原值，再临时执行 `sysctl -w vm.max_map_count=2000000`；验证结束停止本次 BE 后恢复原值，不默认写入永久配置。FE 的 JVM 堆和 BE 的 mem_limit 还需按容器/进程实际内存预算设置，不能根据宿主机总内存直接分配。

BE 启动脚本还检查 `ulimit -n` 至少为 60000，以及 swap 状态。共享测试机不要擅自关闭宿主机 swap。容器可以设置相同的 `--memory` 与 `--memory-swap` 限额来禁止自身使用 swap；若 3.0.8 包装脚本仍因宿主机 swap 而拒绝启动，先独立核对 cgroup v2 的 memory.swap.max 为 0（v1 则 memory.memsw.limit_in_bytes 与 memory.limit_in_bytes 相等），并确认映射上限和文件限制均达标，再使用上游提供的 `SKIP_CHECK_ULIMIT=true` 环境变量启动 BE，记录这一测试条件；不得用它掩盖未满足的资源条件。

Docker 的文件句柄默认值不一定继承宿主机的大限额；创建测试容器时显式设置 `--ulimit nofile=655350:655350`，启动后在同一容器内核对 `ulimit -n`。

```bash
mysql --protocol=TCP --host localhost --port 9030 --user root --execute 'SHOW FRONTENDS; SHOW BACKENDS;'
```

确认目标 FE 的 Join/Alive 与各预期 BE 的 Alive 为真，检查磁盘与日志。容器里的服务地址、广告地址和存储挂载按实际拓扑设置。同机多容器的 CPU quota 与 cpuset 分别记录。

示例命令在 Linux/Bash 下执行。需安装 MySQL 兼容命令行客户端；若系统只提供 mariadb 命令，最小验证可替换命令并给检查脚本传 `--client mariadb`。Linux 上 MySQL 客户端对 localhost 默认可能走 Unix socket 而忽略 `--port`，所有示例显式传 `--protocol=TCP`，检查脚本也始终传该参数。下面 TPC-H 上游脚本直接调用 mysql，因此运行该工具链前需提供可用的 mysql 命令；若 FE_HOST 为 localhost，请在客户端配置文件 `[client]` 段设置 `protocol=TCP` 或改用可达的 FE 地址，避免上游脚本落到本地 socket。

## 最小 SQL 验证

```bash
mysql --protocol=TCP --host localhost --port 9030 --user root --batch --skip-column-names --execute 'SELECT 1'
mysql --protocol=TCP --host localhost --port 9030 --user root --batch --skip-column-names --execute 'SELECT SUM(v) FROM (SELECT 1 AS v UNION ALL SELECT 2 UNION ALL SELECT 3) AS t'
```

期望分别为 `1` 与 `6`。自动检查入口为 `scripts/check_service.py`。这是连接与 SQL 计算检查；集群的数据读写能力还须通过下述建表、导入和查询验证。

## 可公开获取的 TPC-H 工具

使用 [官方 TPC-H 流程](https://doris.apache.org/docs/3.x/benchmark/tpch/) 的完整公开工具目录，不能只下载 bin 中的几个脚本。下面提供 Doris 3.0.8 的固定工具版本例子；其他服务版本使用其实际存在的 tag 并核对工具兼容性，版本号不等于 Git tag 名。两种获取方式都从 skill 根目录开始，并在进入工具目录前设置 `SKILL_DIR`，后续命令沿用该会话变量。

```bash
SKILL_DIR="$(pwd)"          # run this block from the skill root
git clone --depth 1 --filter=blob:none --sparse --branch 3.0.8-rc01 https://github.com/apache/doris.git doris-source
git -C doris-source sparse-checkout set tools/tpch-tools
git -C doris-source rev-parse HEAD
cd doris-source/tools/tpch-tools
```

这个 tag 对应 commit `09b0cc49a60ffdd444df3e40e5f3dc180299b561`；目录包含 bin、conf、ddl 和 queries。运行主机需要 Bash、Git、GNU getopt/coreutils、gcc（该工具例子使用 GCC 14 或更早的兼容 C 工具链）、make、wget、unzip、curl 和 mysql。上游 `bin/build-tpch-dbgen.sh` 会在 `bin/` 下直接下载并解压生成器 ZIP；不要直接运行它，改用下文的校验构建入口，先核对摘要再解压和 make。离线时将网络下载及构建放在相同目标架构的联网构建机，打包整个 tools/tpch-tools 目录转入测试机。

Git 传输不可用时，也可在 skill 根目录使用随附的标准库下载器，仅取得工具目录，避免下载整个仓库。工具目录默认放在 Git 工作树之外；如果放在工作树内，skill 的忽略规则只覆盖本 skill 的 `experiment/` 等示例位置：

```bash
SKILL_DIR="$(pwd)"          # run this block from the skill root
TOOLS_DIR="$HOME/.cache/kpbot-doris/tpch-tools"
python3 "$SKILL_DIR/scripts/fetch_tpch_tools.py" --ref 3.0.8-rc01 --output "$TOOLS_DIR"
cd "$TOOLS_DIR"
```

下载器先将 ref 解析到 commit，再通过 GitHub 的公开 API/Raw 下载完整目录，逐文件校验 Git blob SHA-1，保存 source.json；输出目录已存在时拒绝覆盖。目标机不能访问 API/Raw 时，可在联网机器运行该命令，然后将生成目录整体复制到目标机。下面所有 bin 命令均可在这个工具目录中执行，无需另外创建脚本。

将 conf/doris-cluster.conf 中 FE_HOST、FE_QUERY_PORT、FE_HTTP_PORT、USER、DB 设置为目标实验集群；DB 使用新的隔离库名。PASSWORD 不填写明文，将该文件原有的 PASSWORD 赋值行替换为下面这一行（保留字面量变量引用）：

```bash
export PASSWORD="${DORIS_PASSWORD?set DORIS_PASSWORD explicitly}"
```

在运行下列 bin 命令的同一个 Bash 会话中，用 `read -r -s -p 'Doris password: ' DORIS_PASSWORD; printf '\n'; export DORIS_PASSWORD` 隐藏输入口令，也可由受控环境注入；空口令需要显式设置 `export DORIS_PASSWORD=''`。不要开启 shell 跟踪，不把展开后的配置或凭据写入结果文件。完成后执行 `unset DORIS_PASSWORD MYSQL_PWD`。conf 只保存连接参数，密码由环境变量注入；该 conf 与最小检查脚本的 `--config` 认证文件都应在 Git 工作树之外创建、限制为仅本人可读，并通过 `--config "$DORIS_CLIENT_CONFIG"` 引用，不要对任何版本库执行 `git add`。skill 的忽略规则覆盖 results、experiment、cases 下的 results 以及示例源码目录；它只是防误提交措施，不能代替凭据管理。先阅读建表脚本，确认只作用于该实验库。

SF=1 起步，容量验证后再调整；以下命令接着上面的 Git 或下载步骤，在同一 Bash 会话、tools/tpch-tools 目录执行（`SKILL_DIR` 已在 `cd` 前设置）。先校验并构建生成器，再生成、建表和导入；任何一步失败即停止。下面的 `load-tpch-data.sh` 仅限已确认可信的隔离测试网络；其他环境跳过该行及其 HTTP 响应验收，改用后面的 HTTPS 导入与验收流程：

```bash
set -euo pipefail
source conf/doris-cluster.conf
RUN_DIR="$PWD/runs/$(date +%Y%m%dT%H%M%S)"
TPCH_DATA_DIR="$PWD/bin/tpch-data"
mkdir -p "$RUN_DIR"
python3 "$SKILL_DIR/scripts/build_tpch_dbgen.py" --tools-dir "$PWD"
bash bin/gen-tpch-data.sh -s 1 -c 2
bash bin/create-tpch-tables.sh -s 1
bash bin/load-tpch-data.sh -c 1 2>&1 | tee "$RUN_DIR/load.stdout"
```

`build_tpch_dbgen.py` 在解压和 make 之前，按 [生成器来源清单](tpch-dbgen-provenance.json) 中项目钉定的 SHA-256 校验 ZIP；摘要不符、下载失败或已有未经验证的解压目录都会停止。校验通过后才解压并调用上游构建，把 URL、SHA-256 和源 Apache commit 写入 `bin/.kpbot-tpch-dbgen-verified.json`，同时记入实验记录。该摘要由项目针对钉定 Apache 源码中的 URL 在 TLS 校验下取得并固定，不是上游发布者签名；若上游之后提供独立发布的权威摘要，应改用该摘要。

**导入验收（全部通过后才开始查询计时）。** 先确认八表 COUNT(*) 与生成文件行数一致，再从原始 Stream Load 输出逐项核对 Status、过滤行和导入行；脚本退出码 0 不代表导入成功。用随附校验器聚合 `load.stdout` 中的每个响应，并连接实验库核对八表：

```bash
python3 "$SKILL_DIR/scripts/verify_tpch_import.py" \
  --load-output "$RUN_DIR/load.stdout" --data-dir "$TPCH_DATA_DIR" \
  --database "$DB" --host "$FE_HOST" --port "$FE_QUERY_PORT" --user "$USER" \
  --protocol=TCP --output "$RUN_DIR/import-check"
```

`import-check/result.json` 中 `status` 为 `passed`、八表 `filtered` 全为 0、`loaded` 与 `expected` 一致，且 `count` 与 `expected` 一致后，才允许执行下一步查询。`--data-dir` 从本次生成的 `.tbl`/`.tbl.N` 文件逐行求和得到八表期望值，其他规模不需要手抄 SF1 数字；也可用 `--expected-json` 传入同样来源的计数。SF=1 的 region/nation/supplier/part/partsupp/customer/orders/lineitem 期望行数依次为 5、25、10000、200000、800000、150000、1500000、6001215。任何 Status 不是 Success、过滤行非 0、缺表或计数不一致都必须停止并保留原始响应。

**Stream Load 传输与凭据。** 钉定 commit 的上游 `bin/load-tpch-data.sh` 用 `curl -u` 通过 HTTP 发送口令，并用 `--location-trusted` 把凭据转发给重定向目标；口令在链路上明文传输。该方式只适用于已确认可信、隔离且无窃听风险的测试网络，不得用于跨网段、共享网络或生产网络。其他环境启用 FE/BE HTTPS 后，用随附脚本导入：先请求 FE 的 307 目标，确认目标是 HTTPS 且主机在显式 `--allow-host` 列表中，再用 `--cacert` 验证 FE/BE 证书后发送数据；不使用 `-k`，也不会把口令转发给未校验的重定向目标。口令只从 `DORIS_PASSWORD` 环境变量读取，不进入 argv。

上游 `gen-tpch-data.sh` 对 partsupp、orders、lineitem 生成 `.tbl.1` 到 `.tbl.N`（N 等于 `-c`），其他五表生成单个 `.tbl`。下面的循环同时覆盖这两类文件，按实际文件 `wc -l` 得到 expected rows，为每个文件指定唯一输出目录和以 `${table}_${index}` 形式结尾的 label；`verify_tpch_import.py` 能按该 label 把响应聚合到表。把 `BE_HOSTS` 设为 `SHOW BACKENDS` 中 Alive 的 BE 地址（多个用空格分隔），`DORIS_CA_BUNDLE` 设为签发 FE/BE 证书的 CA 文件：

```bash
export DORIS_PASSWORD   # 已按上文隐藏输入或受控注入
: "${BE_HOSTS:?set BE_HOSTS to Alive BE addresses from SHOW BACKENDS, space separated}"
: "${DORIS_CA_BUNDLE:?set DORIS_CA_BUNDLE to the CA bundle for FE/BE certificates}"
read -r -a ALLOWED_BE_HOSTS <<< "$BE_HOSTS"
ALLOW_ARGS=()
for host in "${ALLOWED_BE_HOSTS[@]}"; do
  ALLOW_ARGS+=(--allow-host "$host")
done
TXID="tpch-$(date +%Y%m%d%H%M%S)"
for table in region nation supplier part partsupp customer orders lineitem; do
  for file in "$TPCH_DATA_DIR/$table.tbl"*; do
    [ -f "$file" ] || continue
    suffix="${file##*.}"
    if [[ "$suffix" =~ ^[0-9]+$ ]]; then index="$suffix"; else index=0; fi
    rows=$(wc -l < "$file")
    label="${TXID}_${table}_${index}"
    python3 "$SKILL_DIR/scripts/stream_load_https.py" \
      --fe-url "https://$FE_HOST:${FE_HTTPS_PORT:-8443}/api/$DB/$table/_stream_load" \
      "${ALLOW_ARGS[@]}" --cafile "$DORIS_CA_BUNDLE" \
      --data-file "$file" --header "column_separator:|" \
      --header "label:${label}" --expected-rows "$rows" \
      --output "$RUN_DIR/https-${table}-${index}" || exit 1
  done
done
python3 "$SKILL_DIR/scripts/verify_tpch_import.py" \
  --responses "$RUN_DIR" --data-dir "$TPCH_DATA_DIR" \
  --database "$DB" --host "$FE_HOST" --port "$FE_QUERY_PORT" --user "$USER" \
  --protocol=TCP --output "$RUN_DIR/import-check-https"
```

循环结束后，校验器把同一表的多个分片响应相加，再与 `--data-dir` 中生成文件的逐表行数核对；八表全部一致才通过。FE 的 307 目标不是 HTTPS、主机不在列表内或证书校验失败时，脚本在发送数据前停止；失败响应、过滤行或导入行不符同样返回非零并保留原始响应。受信隔离网络仍可使用上游脚本，但必须在实验记录中注明明文 HTTP 条件。

单独执行查询计时：只有上面的导入验收通过后才运行下面命令。检查查询结果后，先预热再多轮测量，保存每次脚本输出与查询级耗时。规模、分桶、副本、会话设置、查询集合与集群资源固定。比较参数时一次只改一个变量；内存不足先关联 BE 指标、容器限制与导入/compaction 状态，不能仅凭日志关键字改 OS 参数。

```bash
bash bin/run-tpch-queries.sh -s 1
```

生成数据保存在 `bin/tpch-data/`；不要将已有目录复用成新一轮数据。SF=1 的 DDL 使用单副本并含 DROP TABLE/VIEW，必须使用新的实验库，其他规模的 DDL 另行核对副本数和 BE 数量。该版本生成脚本始终给大表生成器传入分片参数，`-c` 至少取 2，不能使用 1。生成完成必须确认八表文件均存在；后台生成任务失败可能不反映在脚本退出码中，不得只凭退出码进入查询阶段。

查询脚本自带一轮首次运行与两轮热运行，报告的是客户端墙钟毫秒，不能声称每轮都清除了缓存。它在 `bin/result/` 写查询输出，在调用目录追加 `result.csv`，再次运行会清理 bin/result。每次完成后先复制这两个位置和 stdout/stderr 到新的轮次目录，再开始下一次；比较结果时保留完整查询集合，不把失败查询算作零耗时。
