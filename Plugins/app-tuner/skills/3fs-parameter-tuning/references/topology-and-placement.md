# 拓扑与数据放置

> **何时读我**：要动副本数、chain table、HEAD 分布、target 布局，或遇到"带宽突然掉到几十 MiB/s""target 集体 OFFLINE""rotate 后分布不符合预期"这类拓扑相关现象时读本文。

节点角色与示例 IP 见 `SKILL.md`。所有 `admin_cli` 命令统一用：

```bash
ADMIN_CLI="/opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml"
```

---

## 一、副本数与写路径跳数

写路径是链式复制 Client→HEAD→MIDDLE→TAIL，**HEAD 网卡同时承载 RX（收客户端数据）与 TX（转发下一跳），单张 100Gb/s 网卡双工共享**：

| 拓扑 | 总带宽（512m） | 总带宽（2G 稳态） | HEAD 网卡负载 | 利用率 |
|---|---|---|---|---|
| 3 storage / 3 副本（3 跳） | 9681 MiB/s | — | TX 4825 + RX 4835 = 9660 MB/s | 77% |
| 2 storage / 2 副本（2 跳） | **12380 MiB/s** | **10068 MiB/s** | RX 8384 MB/s | 67%（2G）/ ~96%（512m） |

**`--size` 口径必须一致**：同配置 512m 测出 12380、2g 测出 10068 —— 512m 是瞬态峰值、2g 是稳态。**对比时尺寸不同则结论无效。**（压测口径纪律见 `benchmarking.md`。）

单网卡理论上限 ~12.5 GB/s。A 集群实测 HEAD RX 峰值 11369 MB/s / TX 11351 MB/s = **~91% 线速** → 网络已是约束，此时任何 CPU 侧优化都不会涨带宽。

**判断"是不是已经撞网卡"**：用 RDMA 带宽监控取 HEAD 节点的 RX/TX 峰值与均值（用法见 `observability.md`）。峰值判"是否触顶"，均值判"稳态约束"。

---

## 二、HEAD 分布

| 尝试 | 拓扑 | 结果 |
|---|---|---|
| chain table 3：24 条 chain 的 HEAD 均分到 storage1/storage2 各 12 条 | 2 storage | **灾难：41 MiB/s** |
| chain table 1 上分散 HEAD（早期） | 3 storage | 性能下降，已回退 |
| chain table 4：rotate 16 条 chain 使 HEAD 变 8/8/8 | 3 storage | 9559（分散前）/ 9526 / **9615 MiB/s** → **中性**，每节点写入量从 4103/4103/0 变成精确的 **8195 MB 各** |

**2 storage 下 HEAD 分散失败的根因**：两个 storage 节点之间需要**双向 RDMA 转发**（storage1→storage2 与 storage2→storage1），当前 3FS 版本的 RDMA 连接管理不支持：

```
ReliableForwarding.cc:234 ERROR] forward timeout, peer: RDMA://<storage1 RDMA 地址>:8000
transport RDMA://storage2/mlx5_0:1/1186 read/write error notified by epoll
```

**3 storage 的单向链式（HEAD→MIDDLE→TAIL）无此问题。** 结论：**2 storage 环境下不要分散 HEAD**；要分散就用 ≥3 storage。

**HEAD 计数与调整命令**：

```bash
$ADMIN_CLI "list-targets" | grep "^401" | grep "HEAD" | awk '{print $6}' | sort | uniq -c
$ADMIN_CLI "list-chains --table-id 4" | awk 'NR>1 {split($7,a,"("); print substr(a[1],3,5)}' | sort | uniq -c
$ADMIN_CLI "set-preferred-target-order <chainId> <t1> <t2>..."
$ADMIN_CLI "rotate-as-preferred-order <chainId>"
$ADMIN_CLI "list-chains"     # 观察 SYNCING → SERVING
```

（`grep "^401"` 里的 `401` 是该集群的 target_id 前缀，换集群要按实际前缀改。）

**rotate 之后必须核对每节点实际写入量**（不只是 HEAD 计数）：3 storage rotate 到 8/8/8 后每节点精确 8195 MB，这才证明分布真的均衡了。

---

## 三、chain table 与目录绑定

| ChainTableId | 版本 | Chain 数 | 副本 | 描述 | 状态 |
|---|---|---|---|---|---|
| 1 | 1 | 24 | 3 | stage（原 3 storage） | 可用（需启动 storage3） |
| 2 | 26 | 24 | 2 | stage-2replica | 活跃 |
| 3 | 34 | 24 | 2 | stage-2replica-balanced（HEAD 分散） | **不可用**（双向转发） |
| 4 | — | 24 | 3 | `only_chunk_engine=false` | 活跃 |

**切换 chain table 不需要重启服务** —— 不同 table 绑定不同目录，改压测目录即可。这是做拓扑 A/B 最省事的方式：

```bash
# 为某个 chain table 建一个绑定目录
$ADMIN_CLI "mkdir /verify_tmp --chain-table-id 1 --chunk-size 1048576 --stripe-size 16"
# 核对目录实际 layout
$ADMIN_CLI "stat --display-layout ${FIO_DIR}/seq-write.0.0"   # 看 Layout-ChunkSize / StripeSize / Type
```

实测 layout：`Layout-ChunkSize = 1048576`（1 MB）、`Layout-StripeSize = 24`（table 4 目录）/ `16`（另一目录）、`Layout-Type = ChainRange`。

压测时只需换目录变量，服务不动：

```bash
FIO_DIR=${MOUNT_POINT}/fio_2replica            bash scripts/fio_bench.sh seq-write   # chain table 2
FIO_DIR=${MOUNT_POINT}/fio_2replica_balanced   bash scripts/fio_bench.sh seq-write   # chain table 3
```

**做拓扑 A/B 前先 `stat --display-layout` 确认两腿目录绑的是哪张表**，否则会把"目录换错了"当成"拓扑无效"。

---

## 四、2 副本 chain table 重建通用流程

流程：**data_placement 求解 → gen_chain_table 生成三件套 → 批量 create-target → upload-chains → upload-chain-table → set-preferred-target-order → rotate-as-preferred-order**。

命令一律用变量与循环表达，**不要硬编码 target/chain ID 清单**（ID 由生成器按规则算出，硬编码脚本换集群即失效）。

```bash
ADMIN_CLI="/opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml"
N=2                 # storage 节点数
R=2                 # 副本数
NODE_BEGIN=10001    # 第一个 storage node_id
NODE_END=10002      # 最后一个 storage node_id
DISKS=4             # 每节点磁盘数, 必须与实际 target 路径数一致
TARGETS_PER_DISK=6
TP=401              # target_id 前缀
CP=700              # chain_id 前缀
TABLE_ID=<新表 ID>
TABLE_DESC='stage-2replica'
MODEL_OUT="output/DataPlacementModel-v_${R}-b_*-r_*-k_${R}-*"   # 求解器输出目录
OUT="output/gen_${TP}_${CP}"
```

### 4.1 求解数据放置

```bash
python3 data_placement.py -ql -relax -type CR \
  --num_nodes "$N" --replication_factor "$R" --min_targets_per_disk "$TARGETS_PER_DISK"
```

- **2 节点场景必须带 `-ql -relax`**（appsi_highs 求解器），否则无解
- 产物：`output/<model>/incidence_matrix.pickle`（还有 `peer_traffic_map.pickle`、`data_placement.html`）

### 4.2 生成 chain table 三件套

```bash
mkdir -p "$OUT"
python3 gen_chain_table.py --chain_table_type CR \
  --node_id_begin "$NODE_BEGIN" --node_id_end "$NODE_END" \
  --num_disks_per_node "$DISKS" --num_targets_per_disk "$TARGETS_PER_DISK" \
  --target_id_prefix "$TP" --chain_id_prefix "$CP" \
  --incidence_matrix_path output/<model>/incidence_matrix.pickle \
  --output_path "$OUT"
```

产物（三个文件，后续步骤全部从它们循环生成命令）：

| 文件 | 内容 |
|---|---|
| `${OUT}/generated_chains.csv` | 表头 `ChainId,TargetId,TargetId`（TargetId 列数 = 副本数，**列序即副本序，第一列为 HEAD**） |
| `${OUT}/generated_chain_table.csv` | 表头 `ChainId`，每行一个 chain |
| `${OUT}/create_target_cmd.txt` | 每行一条完整 `create-target --node-id … --disk-index … --target-id … --chain-id … --use-new-chunk-engine` |

ID 编码约定（生成器内部规则，便于人工核对，不需要手工计算）：

```
target_id = ((target_id_prefix * 1e6 + node_id) * 1000 + (disk_index + 1)) * 100 + (target_index + 1)
chain_id  = (chain_id_prefix * 1000 + (disk_index + 1)) * 100000 + chain_index
```

- `--num_disks_per_node` **必须与实际 target 路径数一致**（实测 storage 节点是 4：`/storage/data0..3`，对应 `[server.targets] target_paths`）
- `create-target … --use-new-chunk-engine` 会在 per-target `/storage/dataN/3fs/<targetId>/target.toml` 里写 `only_chunk_engine=true`；要 false 就用 `sed` 去掉该 flag（或从 `create_target_cmd.txt` 里删掉这个 flag 再执行）

### 4.3 批量建 target（从命令文件循环，不硬编码）

```bash
# 方式一：整体喂给 admin_cli（token 从文件读，不出现在命令行历史里）
$ADMIN_CLI --config.user_info.token "$(< /opt/3fs/etc/token.txt)" < "${OUT}/create_target_cmd.txt"

# 方式二：逐行执行，便于定位第一条失败命令（推荐，48 条命令里失败一条很难肉眼找）
while IFS= read -r cmd; do
  [ -z "$cmd" ] && continue
  echo ">> $cmd"
  $ADMIN_CLI "$cmd" || { echo "FAILED: $cmd" >&2; break; }
done < "${OUT}/create_target_cmd.txt"
```

也可以用按节点/磁盘批量建的命令（从 routing info 取 target，不需要逐条 ID）：

```bash
for nid in $(seq "$NODE_BEGIN" "$NODE_END"); do
  $ADMIN_CLI "create-targets --node-id ${nid} --disk-index $(seq 0 $((DISKS - 1)) | tr '\n' ' ') --use-new-chunk-engine"
done
```

### 4.4 上传 chain 与 chain table

```bash
$ADMIN_CLI "upload-chains ${OUT}/generated_chains.csv"
# 新建表时可直接把 CSV 列序写成 preferred order（省掉 §4.5 的 set 循环）:
#   $ADMIN_CLI "upload-chains ${OUT}/generated_chains.csv --set-preferred-target-order"
$ADMIN_CLI "upload-chain-table --desc '${TABLE_DESC}' ${TABLE_ID} ${OUT}/generated_chain_table.csv"
```

CSV 格式要求（`upload-chains -d` 可导出模板）：第 0 列必须叫 `ChainId`，其余列必须叫 `TargetId`，行内列数必须与表头一致，TargetId 必须 > 0。

### 4.5 设置 preferred order 并 rotate（从 CSV 循环）

CSV 每行的 TargetId 列序就是期望的副本顺序（第一列 = 期望 HEAD）。**HEAD 均衡靠"相邻行的第一列交替属于不同节点"实现**，生成器已经排好，脚本只需照抄列序：

```bash
tail -n +2 "${OUT}/generated_chains.csv" | while IFS=, read -r chain_id replicas; do
  [ -z "$chain_id" ] && continue
  $ADMIN_CLI "set-preferred-target-order ${chain_id} ${replicas//,/ }"
  $ADMIN_CLI "rotate-as-preferred-order ${chain_id}"
done

# 收敛观察：SYNCING → SERVING
$ADMIN_CLI "list-chains --table-id ${TABLE_ID}"
```

只设顺序不 rotate 不会改变实际 HEAD；只 rotate 不设顺序等于按当前顺序轮转。两条都要。

> **2 storage 环境不要执行 HEAD 均衡**（§二 的 41 MiB/s 结论）。此时 CSV 应保持所有行第一列同属一个节点。

### 4.6 target 清理的坑

- `remove-target` 报 `target is still in a chain!`
- `update-chain <cid> <tid> -m remove` 要求 target 先 OFFLINE
- `rotate-lastsrv` 只能轮转 LASTSRV、到不了 OFFLINE

**可行做法**：跳过 mgmtd 链清理，直接 `remove-target --node-id <nid> --target-id <tid> --force`，旧 chain 留在 mgmtd 里标 UNAVAILABLE（无害）。批量写法同样从实际状态循环，**不要猜 node_id**：

```bash
# 1) 导出当前 target 清单, 从中取 (target_id, node_id) 两列生成映射文件
#    列位置以本机 list-targets 的表头为准(不同版本列序可能不同), 先看清再 awk
$ADMIN_CLI "list-targets" > /tmp/targets.txt
awk -v p="$TP" 'NR>1 && $1 ~ ("^" p) {print $1, $2}' /tmp/targets.txt > /tmp/target_node.map

# 2) 循环 force 删除(单条失败不阻断, 便于事后核对残留)
while read -r tid nid; do
  [ -z "$tid" ] && continue
  echo ">> remove-target --node-id ${nid} --target-id ${tid} --force"
  $ADMIN_CLI "remove-target --node-id ${nid} --target-id ${tid} --force" || true
done < /tmp/target_node.map

# 3) 核对残留
$ADMIN_CLI "list-targets" | grep -c "^${TP}"
```

---

## 五、时钟同步（会导致伪故障）

**事故链**：storage3 时钟慢 **34 秒** > `heartbeat_timestamp_valid_window='30s'` →

```
Mgmtd::HeartbeatFail(5002) Too much timestamp deviation
  (MgmtdClient.cc:183/399, HeartbeatOperation.cc:134-141)
→ mgmtd 把该节点全部 target 标 OFFLINE
→ TargetMap.cc:329-341 强制 localState OFFLINE
→ rotate 期间 RoutingVersionMismatch(7014) request ChainVersion(852) != local 851
→ HEAD 分布变成 16/8/0（预期 8/8/8）
```

**修复**：`chronyc makestep`（或 `ntpdate`）校时到差 <1 ms，再重做 rotate → 每节点精确 8195 MB。

mgmtd 心跳相关项（`mgmtd_main.toml`）：`heartbeat_timestamp_valid_window='30s'`、`heartbeat_fail_interval='1min'`、`send_heartbeat_interval='10s'`、`heartbeat_ignore_stale_targets=true`。

**教训**：拓扑调整失败时先查时钟，再查配置。时钟偏差还会污染 ClickHouse 查询窗口（见 `observability.md` §时钟偏移校正）。

```bash
chronyc tracking; chronyc sources -v      # 或 ntpstat / timedatectl
for h in <storage1> <storage2> <storage3> <meta>; do
  echo "== $h"; ssh -o StrictHostKeyChecking=no root@"$h" "date +%s.%N"
done
```

---

## 六、拓扑调整纪律

1. **先核实服务实际状态**：`systemctl status storage_main` / `list-nodes` / `list-targets`。曾出现"用户认为某 storage 已停机、实际 active (running) 数月"的误记，据此推出的拓扑结论全错。
2. **一次只动一个变量**：换 chain table 与改配置不要同一轮做。
3. **改完配置同步各节点 `/opt/3fs/etc/`**（见 `config-mechanics.md` §1.5），否则重启后回到旧配置。
4. **A/B 两腿口径一致**：`--size`、fio 版本、二进制 md5、ConfigVersion、chain table、目录 layout 全部对齐。
5. **失败先回退再分析**：拓扑类改动一旦劣化（如 41 MiB/s），先切回原 chain table 目录恢复业务，再 offline 分析日志。
