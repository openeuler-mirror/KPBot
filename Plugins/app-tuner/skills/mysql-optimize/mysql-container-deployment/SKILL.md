---
name: mysql-container-deployment
description: >
  MySQL 容器形态部署子技能（Kunpeng/aarch64 与 x86_64 Linux）：supermin 制作轻量镜像、
  docker import、`docker run` 的 `--cpuset-cpus/--cpuset-mems/--memory/--net=host/--privileged/-v` 参数、宿主机网卡 IRQ 与容器核映射、容器内 mysqld 绑核、按线程 TID 识别
  worker/log 线程并设置亲和、Point/RO/RW/WO 线程数基线、写场景 IO 归零等待、验收与回滚。
  用于 2C8G/8C16G/8U32G/32C256G 容器 POC 与性能验收；不负责编译（→ mysql-build-deployment）、
  参数深度调优（→ mysql-app-config-optimization）、压测判定（→ mysql-test-verification）。
  触发词：MySQL 容器、docker、supermin、cpuset、cpuset-mems、容器绑核、容器网络中断、
  8U32G 容器、容器压测。
license: MulanPSL-2.0
compatibility: Linux aarch64/x86_64；openEuler 22.03/24.03、velinux Debian10 实测；docker 18.09+；host 需 root 或 docker 权限
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: relational_database
  version: "1.0.0"
---

# MySQL 容器形态部署

## 简介

在宿主机上制作/导入轻量容器镜像，以固定 cpuset/mems/memory 启动容器，在容器内运行
MySQL 单实例，并把宿主机 IRQ、容器 CPU、mysqld worker/log 线程的绑核关系固定下来，
使小规格（2C8G/8C16G/8U32G/32C256G）POC 结果可复现。

**实测基线**：Kunpeng 920 系列 + openEuler/velinux，容器内 8U32G，MySQL 8.0.25/8.0.28
（Percona 5.7.44 流程类似）；压测客户端在另一台机器（远端压测）。

## 何时触发

- 需要搭建 8U32G（或其他规格）MySQL 容器并做性能/功能验收。
- 需要固定容器 CPU/NUMA、把宿主 IRQ 绑到容器映射核、在容器内绑 mysqld 线程。
- 容器内 CPU 打不满、P99 抖动、写场景越跑越慢，需要核对 cpuset/IRQ/IO。
- 需要可复现的容器启动参数与验收记录。

**不适用**：源码编译/初始化 → `mysql-build-deployment`；my.cnf 深度调参 →
`mysql-app-config-optimization`；压测口径/A-B → `mysql-test-verification`；
虚机形态 → `mysql-vm-deployment`。

## 变量约定

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${IMAGE_NAME}` | 容器镜像名 | `mysql-base` |
| `${CONTAINER_NAME}` | 容器名 | `mysql-8u32g` |
| `${CPUSET}` | 容器 CPU 列表（宿主编号，如 80-87） | `80-87` |
| `${MEMS}` | 容器 NUMA 节点（支持 `0,1`/`0-3` 列表与区间） | `1` |
| `${MEM_LIMIT}` | 内存限制 | `32g` |
| `${MYSQL_HOME}` | 容器内安装前缀（宿主映射） | `/opt/mysql-8.0.25` |
| `${DATA_ROOT}` | 容器内数据目录（宿主映射） | `/data/mysql` |
| `${HOST_CONF}` | 宿主上选定的实例配置文件（核验后只读挂载） | `/data/mysql/conf/my.cnf` |
| `${MYSQL_CONF}` | 容器内 my.cnf（启动命令实际读取的路径） | `/etc/my.cnf` |
| `${HOST_ETH}` | 宿主业务网卡 | `eth0` |
| `${IRQ_CORE}` | 宿主 IRQ 绑定的核（= 容器首核） | `80` |
| `${WORKER_CORES}` | 容器业务核（由 Cpus_allowed_list 推导，去掉首核及 log 专用核 81/82） | `83-87` |
| `${CLIENT_HOST}` | 压测客户端 | `<客户端地址>` |
| `${MYSQL_PORT}` | MySQL 端口（预检/防火墙放通） | `3306` |
| `${WRITER_CORE}` / `${FLUSHER_CORE}` | log_writer / log_flusher 绑定核 | `81` / `82`（首核+1/+2，在 worker 83-87 之外） |
| `${SOCKET}` | 容器内 mysql socket（供 performance_schema 查询 TID） | `${DATA_ROOT}/mysql.sock` |
| `${ALLOWED}` / `${START}` / `${END}` | 容器 Cpus_allowed_list 与其区间端点（脚本内派生） | `80-87` / `80` / `87` |
| `${BUS_INFO}` / `${IRQ_LIST}` | 网卡 bus-info / 改队列后重查的 IRQ 号列表（脚本内派生） | — |
| `${PID}` | mysqld 主进程号（脚本内派生） | — |
| `${irq}` / `${tid}` / `${i}` / `${N}` | 循环局部变量（无需外部传入） | — |

## 工作流总览

```
宿主前置检查 → 制作/导入镜像 → docker run（cpuset/mems/memory/host net/volume）
  → 宿主 IRQ 绑核 → 容器内启动 mysqld → 连接/参数验收
  → 压测建连后按线程 TID 绑 worker/log → 复测 → 记录
```

原则：

- 容器 CPU 必须来自同一 NUMA；`--cpuset-cpus` 与 `--cpuset-mems` 同时给。
- 宿主 IRQ 落在容器 cpuset 之外（或容器首核（80），按规格），避免与业务抢核。
- 每个容器实例独立 port/socket/datadir，避免同机多实例冲突。

## 镜像制作（supermin）

```
# 宿主安装
yum install -y supermin* docker

# 准备基础包（按需加入 perf/vim/sysstat/numactl）
supermin -v --prepare bash coreutils yum vim sysstat net-tools numactl \
  perf libaio libatomic procps-ng -o supermin.d

# 构建 rootfs 并打包
supermin -v --build --format chroot supermin.d -o appliance.d
tar --numeric-owner -cpf "${IMAGE_NAME}.tar" -C appliance.d/ .

# 导入 docker
cat "${IMAGE_NAME}.tar" | docker import - "${IMAGE_NAME}"
docker images | grep "${IMAGE_NAME}"
```

- `--prepare` 只记录包集合，`--build` 才生成 rootfs；内核使用宿主内核。
- 镜像内不放密码/业务数据；数据通过 volume 映射。
- 也可使用发行版自带镜像；关键是包与内核模块匹配。

## 启动容器

```
# 先核验选定的实例配置：必须是真实存在的普通文件，且就是准备交给 mysqld 的那份
test -f "${HOST_CONF}" || { echo "配置文件不存在：${HOST_CONF}" >&2; exit 1; }
grep -vE '^\s*(#|$)' "${HOST_CONF}" | head -n 20

# 只读挂载到启动命令实际读取的 ${MYSQL_CONF} 路径，避免容器读到镜像自带配置
docker run -itd --privileged=true --net=host \
  -v "${MYSQL_HOME}:${MYSQL_HOME}" \
  -v "${DATA_ROOT}:${DATA_ROOT}" \
  -v "${HOST_CONF}:${MYSQL_CONF}:ro" \
  --cpuset-cpus "${CPUSET}" \
  --cpuset-mems "${MEMS}" \
  --memory "${MEM_LIMIT}" \
  --ulimit nofile=102400:102400 \
  --name "${CONTAINER_NAME}" "${IMAGE_NAME}" /bin/bash

docker start "${CONTAINER_NAME}"
docker exec -it "${CONTAINER_NAME}" /bin/bash
```

- 挂载源必须是核验过的实例配置文件，目标必须是启动命令 `--defaults-file` 使用的
  `${MYSQL_CONF}`（默认 `/etc/my.cnf`），加 `:ro` 防止容器内误改。仅映射安装前缀和
  数据目录会让 mysqld 读不到该配置、转而读取镜像内其他 my.cnf。
- 容器内 CPU 沿用宿主编号（示例 cpuset 80-87 时容器内就是 80-87，不是 0-7）。进容器后
  用 `grep Cpus_allowed_list /proc/self/status` 取真实可用核再决定绑核参数，不要凭
  `nproc` 假设从 0 开始。

参数说明：

| 参数 | 作用 | 注意 |
| --- | --- | --- |
| `--cpuset-cpus` | 容器可用 CPU | 必须是宿主同一 NUMA 的连续/列表核 |
| `--cpuset-mems` | 容器内存节点 | 与 cpuset 对应；避免跨 NUMA |
| `--memory` | 内存上限 | buffer pool 必须留余量 |
| `--net=host` | 共享宿主网络 | 简化压测；端口冲突需自查 |
| `--privileged` | 允许 perf/挂载等 | 仅测试环境；生产收紧 |
| `-v` | 映射安装/数据/配置 | 路径必须存在且权限正确 |
| `-v "${HOST_CONF}:${MYSQL_CONF}:ro"` | 只读挂载选定 my.cnf | 源先核验，目标必须是启动 `--defaults-file` 用的路径 |
| `--ulimit nofile` | 文件描述符 | 与 my.cnf `open_files_limit` 匹配 |

## 宿主 IRQ 与容器核映射（完整命令见 `references/container-deploy.md` §4）

变更前保存队列数/IRQ 清单/各 IRQ 亲和掩码供回滚，停 irqbalance；调整队列数后必须
**重新读取** `/proc/interrupts` 取新 IRQ 号（旧号失效），逐个写 `smp_affinity_list`
绑 `${IRQ_CORE}` 并回读 `effective_affinity_list`。结束时恢复原队列数、原掩码与 irqbalance。
8U32G 常见布局为容器首核兼 IRQ、业务用 83-87（81/82 留给 log 线程）；多队列机型按实测调整。
不要停宿主 firewalld，远端压测只对授权客户端的 `${MYSQL_PORT}` 加 rich rule，测后删除并 reload。

## 容器内启动与绑核（完整命令见 `references/container-deploy.md` §5/§6）

先读 `Cpus_allowed_list` 得到容器真实核号（沿用宿主编号，示例 80-87，不是 0-7），
按该集合 `numactl -C` 启动 mysqld；压测**建连后**按 `performance_schema.threads.NAME`
查出 `THREAD_OS_ID`（即 `/proc/<pid>/task` 下的 TID），用系统自带 `taskset -pc` 把
`thread/sql/one_connection` 的连接线程轮流绑到 `${WORKER_CORES}`（83-87）、写线程绑
`${WRITER_CORE}`/`${FLUSHER_CORE}`（81/82，worker 集合之外），最后用
`taskset -pc`/`ps -T ... -o psr` 回读核对。`comm` 线程名仅 8.0.28+ 且
`performance_schema=ON` 时存在，8.0.25 恒为 `mysqld`，不能作为识别依据（见
`references/container-deploy.md` §5）。全程不依赖任何外部脚本。

线程数基线（8U32G 容器，来自实测）：

| 场景 | Kunpeng 基线 | 竞品/多队列基线 |
| --- | --- | --- |
| Point | 28 | 40 |
| RO | 28 | 40 |
| RW | 80 | 100 |
| WO | 80 | 100 |

- 以上是“容器核少 + 首核（80）兼 IRQ”的起点；以 CPU 打满且 IRQ 核不饱和为准。
- worker 总数应与客户端并发一致；先建连再绑核，避免绑到尚未创建的线程。
- 写场景每轮结束等磁盘 IO 归零、mysqld CPU 降下来再开始下一轮。

## 验收

| 检查 | 命令 | 通过标准 |
| --- | --- | --- |
| 容器 | `docker ps` | Up，cpuset/mems 与规划一致 |
| CPU | `docker inspect -f '{{.HostConfig.CpusetCpus}}'` | 等于 `${CPUSET}` |
| 进程 | `docker exec ... ps -ef \| grep [m]ysqld` | 单主进程 |
| 连接 | `mysql --socket=... -e 'SELECT 1'` | 返回 1 |
| 绑核 | `taskset -pc <tid>` / `ps -Lo psr` | 业务线程落在 `${WORKER_CORES}`（如 83-87），log 线程在 81/82，不与 IRQ 核 80 冲突 |
| IRQ | `/proc/interrupts` 增量 | IRQ 落在规划核，且不挤占业务 |
| 压测 | 客户端 sysbench | 3 轮均值，记录 TPS/QPS/P95/CPU |

## 常见问题

| 症状 | 处置 |
| --- | --- |
| 容器 CPU 与宿主 NUMA 不匹配 | 同时设置 `--cpuset-cpus` 与 `--cpuset-mems`，核对 `lscpu` |
| IRQ 抢占业务核 | 把宿主 IRQ 移到容器 cpuset 外，或按 8U 布局让容器首核（80）专职 IRQ |
| 容器内 `perf` 不可用 | 需要 `--privileged`/`--cap-add` 与宿主 perf 权限；否则用宿主 perf -p |
| buffer pool OOM | `--memory` 与 buffer pool 不匹配；buffer pool 降到 50%–70% |
| nofile 不足 | 同时调 `--ulimit nofile` 与 my.cnf `open_files_limit` |
| 写场景越跑越慢 | 等 IO 归零；检查脏页/checkpoint/`innodb_io_capacity` |
| 结果波动大 | 固定 cpuset、隔离子机、预热、数据快照、3 轮均值 |

## 安全与边界

- `--privileged` 仅用于测试/POC；生产容器应最小权限，并显式挂载所需设备。
- 不在镜像/脚本/命令行写密码；用挂载的 `--defaults-extra-file`（600）。
- 不修改宿主内核/BIOS；需要时交给 `mysql-os-optimization`/`bios-optimization`。
- 删除容器前确认数据在宿主 volume 中且已备份。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/container-deploy.md` | 镜像、run 参数、IRQ/绑核、线程基线、故障与回滚细节 |
| `scripts/preflight_container.sh` | 容器部署前只读检查（docker/cpuset/mems/内存/端口/镜像/挂载） |
