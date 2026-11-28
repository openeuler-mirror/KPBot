# 容器部署细节

## 1. 镜像方式对比

| 方式 | 优点 | 缺点 | 适用 |
| --- | --- | --- | --- |
| supermin 制作 | 轻量、可裁剪、与宿主内核一致 | 需要 supermin；包集合需维护 | POC/性能测试 |
| 发行版基础镜像 | 简单、生态好 | 体积大、包多 | 通用 |
| 应用镜像（含 MySQL） | 开箱即用 | 版本/参数固定，难定制 | 快速验证 |

supermin 生成的 rootfs 使用宿主内核，容器内不能加载不同内核模块；需要 perf/ebpf 时
确认宿主内核与工具匹配。

## 2. 目录与权限

```
${MYSQL_HOME}/          # 二进制（宿主目录映射，只读更安全）
${DATA_ROOT}/data/      # datadir
${DATA_ROOT}/log/       # 错误/慢日志
${DATA_ROOT}/tmp/       # tmpdir
${DATA_ROOT}/run/       # pid/socket
${HOST_CONF}            # 选定的实例配置（核验后 :ro 挂载到 ${MYSQL_CONF}）
```

- 容器内运行用户 uid/gid 必须与宿主目录属主一致，否则权限拒绝。
- 不要把 datadir 放在容器可写层；容器删除会丢数据。
- 多容器共享宿主目录时，每个实例独立子目录与端口。
- 只映射 `${MYSQL_HOME}`/`${DATA_ROOT}` 时，mysqld 读不到实例配置：必须把核验过的
  `${HOST_CONF}` 只读挂载到启动命令 `--defaults-file` 实际读取的 `${MYSQL_CONF}`
  （默认 `/etc/my.cnf`），避免误读镜像自带配置导致启动失败或参数不符。

## 3. cpuset / mems / memory 规划

```
lscpu -p=CPU,NODE | grep -v '^#'
cat /sys/devices/system/node/node*/meminfo | grep MemTotal
```

- 8U32G：选同一 NUMA 的 8 个连续核；内存节点与核节点一致。
- 32C256G：可跨两个 NUMA，但需评估跨片延迟；优先单节点。
- `--memory` 必须大于 buffer pool + 连接/排序/临时表/OS 余量；8U32G 用 22G buffer pool
是实测值，不是上限。
- `--cpuset-mems` 缺省时容器可能跨节点分配内存，导致延迟抖动；该参数接受逗号列表与
  区间（如 `0,1` 或 `0-3`），32C256G 跨两 NUMA 时按此传入，preflight 逐项校验范围。
- 容器内 CPU 编号沿用宿主编号：`--cpuset-cpus 80-87` 时容器内
  `Cpus_allowed_list` 就是 `80-87`，不是 0-7；`numactl -C`、`taskset -pc`
  与 IRQ 绑核必须统一按该真实集合取值，不能写死 0-7。

## 4. 宿主 IRQ 布局

| 规格 | IRQ 队列 | IRQ 核 | 业务核 | 说明 |
| --- | --- | --- | --- | --- |
| 8U32G（Kunpeng 单队列） | 1 | 容器首核 80 | 83-87（81/82 留给 log 线程） | 首核兼 IRQ，最后加线程压满 |
| 8U32G（多队列/竞品） | 8 | 容器核 80-87 内不同核 | 83-87（81/82 留给 log 线程） | 需实测 IRQ 是否挤占业务 |
| 2C8G | 1 | 容器首核（如 80） | 次核 81 | 核少，业务与 IRQ 共享不可避免 |

命令：

```
BUS_INFO="$(ethtool -i "${HOST_ETH}" 2>/dev/null | awk '/^bus-info:/{print $2}')"
[ -n "${BUS_INFO}" ] || { echo "未取得 bus-info：${HOST_ETH}" >&2; exit 1; }

# IRQ 号优先从 sysfs 直取：/proc/interrupts 的名称是否含 PCI 地址因驱动而异
irq_list() {
  local dir="/sys/class/net/${HOST_ETH}/device/msi_irqs"
  if [ -d "${dir}" ]; then
    local out; out="$(ls "${dir}" 2>/dev/null)" || return 1
    [ -n "${out}" ] && { printf '%s\n' "${out}"; return 0; }
  fi
  grep -F "${BUS_INFO}" /proc/interrupts | awk -F: '{gsub(/ /,"",$1);print $1}'
}

# 1) 变更前保存原状态（队列数、IRQ 清单、各 IRQ 亲和），供回滚核对
ethtool -l "${HOST_ETH}" | grep -i combined
irq_list > irq_before.txt
[ -s irq_before.txt ] || { echo "未枚举到 IRQ，禁止继续绑核" >&2; exit 1; }
for irq in $(cat irq_before.txt); do
  printf '%s=%s\n' "${irq}" "$(cat "/proc/irq/${irq}/smp_affinity_list")"
done > affinity_before.txt
systemctl stop irqbalance

# 2) 定位 NUMA 与首轮 IRQ
lspci -v -s "${BUS_INFO}" | grep -i numa
grep -F "${BUS_INFO}" /proc/interrupts

# 3) 调整队列数；驱动会重建 IRQ，旧 IRQ 号随即失效
ethtool -L "${HOST_ETH}" combined 1

# 4) 调整后重新查询当前队列的 IRQ，不能沿用第 2 步的旧号
IRQ_LIST="$(irq_list)"
[ -n "${IRQ_LIST}" ] || { echo "调整队列后未枚举到 IRQ，禁止绑核" >&2; exit 1; }

# 5) 逐个绑定并回读核对实际亲和
for irq in ${IRQ_LIST}; do
  echo "${IRQ_CORE}" > "/proc/irq/${irq}/smp_affinity_list"
  printf 'irq=%s requested=%s effective=%s\n' \
    "${irq}" "${IRQ_CORE}" "$(cat "/proc/irq/${irq}/effective_affinity_list")"
done
```

- bus-info 必须保留完整 PCI 地址（`0000:7d:00.0`），按 `awk -F:` 截首段只会得到
  `0000`，后续 `lspci`/`grep` 全部失真；`grep -F` 避免把地址当正则。
- IRQ 号优先取 `/sys/class/net/${HOST_ETH}/device/msi_irqs/`；该目录不存在或为空时
  才用 `grep -F "${BUS_INFO}" /proc/interrupts` 兜底。IRQ 名称是否含 PCI 地址因驱动
  而异（鲲鹏 920 的 hinic/hns3 等多数不含），按地址 grep 为空属正常，此时需按驱动名
  在 `/proc/interrupts` 中核对，不得把空清单当成“无需绑核”。
- 改队列数会重建 IRQ，必须在调整后重新枚举并回读 `effective_affinity_list` 核对实际亲和，
  不能沿用旧 IRQ 号或假设设置已生效。
- 多队列时逐队列绑不同核，避免全部落一个核。
- **不要停止宿主 firewalld**：IRQ 绑核无需改防火墙。远端压测需放通时只对授权客户端的
  MySQL 端口加 rich rule，测试后删除规则并 reload 恢复，不对全网/全部端口放通。
- 结束测试恢复 irqbalance/原队列数/`affinity_before.txt` 中的原掩码，并记录。

## 5. 容器内绑核（performance_schema 识别 TID，系统命令）

不能用 `/proc/<pid>/task/*/comm` 匹配线程名：8.0.25 的 `sql/`/`storage/` 没有
`pthread_setname_np`/`PR_SET_NAME`，连接线程与 InnoDB 后台线程的 `comm` 恒为 `mysqld`；
8.0.28 起才由 `performance_schema` 命名（`connection`、`ib_*`），且
`performance_schema=OFF` 时仍全空。匹配不到时两个循环一次都不执行、`taskset` 不调用，
脚本仍返回 0，会把「未绑核」误当「已绑核」。

统一按 `performance_schema.threads.NAME` 取 `THREAD_OS_ID`（即 `/proc/<pid>/task` 下的
TID）再绑核；后台线程名逐版不同，以实例查询为准：

```
# 前提：实例以 performance_schema=ON 启动（8.0 默认 ON，压测常关，需显式确认）
mysql -S "${SOCKET}" -uroot -N -B -e "
SELECT THREAD_OS_ID FROM performance_schema.threads
 WHERE NAME='thread/sql/one_connection' AND PROCESSLIST_ID IS NOT NULL" \
  > /tmp/conn_tids.txt
[ -s /tmp/conn_tids.txt ] || { echo "未查到连接线程 TID：确认 performance_schema=ON 且已建连" >&2; exit 1; }

# 连接线程轮流绑到 worker 核 83-87（首核 80 兼 IRQ，81/82 留给 log 线程）
i=0; N=5
while read -r tid; do
  if [ -n "${tid}" ]; then
    taskset -pc "$((83 + i % N))" "${tid}" >/dev/null
    i=$((i+1))
  fi
done < /tmp/conn_tids.txt

# 写场景：后台线程按 NAME 取 TID，单独绑到 81/82（在 worker 集合之外）
mysql -S "${SOCKET}" -uroot -N -B -e "
SELECT THREAD_OS_ID FROM performance_schema.threads
 WHERE NAME LIKE 'thread/innodb/log_writer%'" \
  > /tmp/log_writer_tids.txt
[ -s /tmp/log_writer_tids.txt ] || { echo "未查到 log_writer TID：确认 performance_schema=ON 且写线程已启动" >&2; exit 1; }
n_writer=0
while read -r tid; do
  if [ -n "${tid}" ]; then
    taskset -pc "${WRITER_CORE}" "${tid}" >/dev/null
    n_writer=$((n_writer+1))
  fi
done < /tmp/log_writer_tids.txt
[ "${n_writer}" -eq "$(wc -l < /tmp/log_writer_tids.txt)" ] || { echo "log_writer taskset 执行数与 TID 数不一致" >&2; exit 1; }

mysql -S "${SOCKET}" -uroot -N -B -e "
SELECT THREAD_OS_ID FROM performance_schema.threads
 WHERE NAME LIKE 'thread/innodb/log_flusher%'" \
  > /tmp/log_flusher_tids.txt
[ -s /tmp/log_flusher_tids.txt ] || { echo "未查到 log_flusher TID：确认 performance_schema=ON 且写线程已启动" >&2; exit 1; }
n_flusher=0
while read -r tid; do
  if [ -n "${tid}" ]; then
    taskset -pc "${FLUSHER_CORE}" "${tid}" >/dev/null
    n_flusher=$((n_flusher+1))
  fi
done < /tmp/log_flusher_tids.txt
[ "${n_flusher}" -eq "$(wc -l < /tmp/log_flusher_tids.txt)" ] || { echo "log_flusher taskset 执行数与 TID 数不一致" >&2; exit 1; }

# 回读核对：taskset -pc <tid> 与 ps -T -p <pid> -o tid=,psr=
# 注：8.0.28 的 flusher OS 名是 ib_log_flush，不是 ib_log_flusher
```

- 空结果必须报错退出，不得静默成功：查不到 TID 时先确认 `performance_schema=ON`、
  socket/账号参数与是否已建连，再重试；`taskset` 执行条数须与查出的 TID 数一致。
- 线程数不能被核数整除时，前面的核多分一个（取模分配自然满足）。
- 必须在压测建连后执行；建连前连接线程不存在（`PROCESSLIST_ID` 为空），绑定无效。
- 核少（2C/4C）时不要单独绑 log，避免业务核不足。

## 6. 压测流程（容器）

```
# 1) 容器内取真实可用核号（示例 Cpus_allowed_list=80-87）并按该集合启动 mysqld
ALLOWED="$(awk '/Cpus_allowed_list/{print $2}' /proc/self/status)"
numactl -C "${ALLOWED}" "${MYSQL_HOME}/bin/mysqld" --defaults-file="${MYSQL_CONF}" &

# 2) 客户端建连（以 Point 28 线程为例）
sysbench ... oltp_read_only --threads=28 --time=120 run

# 3) 建连完成后按 §5 的 P_S 查询取 TID、taskset 循环把连接线程绑到 83-87（无需外部脚本）
mysql -S "${SOCKET}" -uroot -N -B -e "
SELECT THREAD_OS_ID FROM performance_schema.threads
 WHERE NAME='thread/sql/one_connection' AND PROCESSLIST_ID IS NOT NULL"   # 先确认已建连

# 4) 加线程到 30，让首核 80 接近满载
sysbench ... oltp_read_only --threads=30 --time=120 run

# 5) 每轮 3 次取均值；写场景等 IO 归零
```

- 同一轮内不要改 cpuset/IRQ；A/B 只改一个变量。
- 客户端在另一台机器；同机 loopback 测不出网络/IRQ 收益。
- 记录：容器规格、cpuset/mems、IRQ 核、worker 核、线程数、轮数、TPS/QPS/P95/CPU。

## 7. 回滚

1. 停压测 → 停 mysqld → 停容器。
2. 恢复宿主 IRQ：队列数、`affinity_before.txt` 里的原掩码、irqbalance；若曾为远端客户端
   添加过 MySQL 端口 rich rule，则删除该规则并 `firewall-cmd --reload`，不要停/启防火墙服务。
3. 恢复 my.cnf/启动脚本备份。
4. 数据目录保留；删除容器前确认 volume 已备份。
5. 记录回滚原因与残留项。

## 8. 安全

- `--privileged` 会放开设备/内核能力，仅测试环境使用；生产用最小 `--cap-add`。
- 不在镜像/命令行写密码；用 600 权限的 `--defaults-extra-file`。
- 不使用 `--net=host` 时注意端口映射与防火墙；压测端口需可达。若需放通，只对已授权
  客户端的 MySQL 端口添加规则并记录删除方式，绝不停用宿主 firewalld 或对全网放通。
- 不删除未知容器/镜像；操作前 `docker ps -a`/`docker images` 确认。

<details>
