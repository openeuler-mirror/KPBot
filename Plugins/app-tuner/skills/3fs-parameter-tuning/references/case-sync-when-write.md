# 案例：`sync_when_write=false` 完整调参闭环（+26.8%）

> **何时读我**：需要一份"标准调参动作序列"模板时读我 —— 从火焰图证据到备份、改参、下发、重启、回读、性能对比、回退、风险评估、后续方向的完整闭环。本文是 `config-mechanics.md` §二 流程的实例化，动作序列可直接照搬到其它参数。

参数细节与风险边界见 `config-mechanics.md` §3.1；节点角色与示例 IP 见 `SKILL.md`。

---

## 一、背景与证据

火焰图分析显示 storage 节点的 RocksDB/WAL 同步开销占 **23.6%**，主要集中在：

```
rocksdb::DBImpl::Write -> WriteToWAL -> WritableFileWriter::Sync -> fdatasync
```

`sync_when_write = true` 导致每次写都执行 fdatasync，严重影响写性能。

**动手前的两项前置确认**（本案例都做了）：

1. 瓶颈在软件配置面，不是代码/硬件 —— 火焰图归因与裸盘 fdatasync 延迟测量归 skill `3fs-hotspot-code-optimization`；本集群 NVMe 固件为 1059，`direct=1 + fdatasync` 均值 2.3 µs、p99 40~42 µs（固件长尾排查见 `hw-and-firmware.md`）
2. 对照 `lever-ranking.md`：该杠杆排第 2 位（+26.8%），风险"高：掉电丢失未 fdatasync 的 WAL" → **必须先与用户确认数据安全性要求**

---

## 二、操作步骤

### 1. 备份当前配置

```bash
ADMIN_CLI="/opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml"
$ADMIN_CLI "get-config --node-type STORAGE -o /tmp/storage_current.toml"
cp /tmp/storage_current.toml "${BACKUP_DIR:-/tmp}/STORAGE_config_v15_$(date '+%Y%m%d_%H%M%S').toml"
```

导出的这份就是 ConfigVersion 15 的运行时真相，回滚以它为准（不要回滚到节点本地 `/opt/3fs/etc/storage_main.toml`，两者可能不一致）。

### 2. 修改配置

```bash
sed 's/sync_when_write = true/sync_when_write = false/' /tmp/storage_current.toml > /tmp/storage_new.toml
```

> 本案例用 `sed` 是因为 `sync_when_write` 在该文件里需要精确定位到 `[server.targets.storage_target.kv_store]` 段（`meta_store` 段也有同名项）。**推荐用段落感知脚本**，见下面第 3 步的等价命令。

### 3. 上传新配置到 mgmtd

```bash
$ADMIN_CLI "set-config --type STORAGE --file /tmp/storage_new.toml --desc 'sync_when_write=false'"
# 输出: Succeed / ConfigVersion 16
```

**第 1~3 步 + 回读校验的等价一条命令**（段落感知、幂等、自动备份、自动回读）：

```bash
bash scripts/set_config_param.sh STORAGE server.targets.storage_target.kv_store sync_when_write false
```

### 4. 重启 storage 服务

`sync_when_write` 虽在源码里声明为热更新项，**实测需重启 storage 才生效**：

```bash
read -rs SSHPASS; export SSHPASS          # 密码只从 stdin 注入, 不落盘
for h in <storage1> <storage2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart storage_main"
done
```

### 5. 重启 FUSE 客户端

FUSE 应用声明配置不可热更新，改 storage 侧参数后同样按顺序重启客户端（顺序：storage 全部节点 → 所有 FUSE 客户端）：

```bash
systemctl restart hf3fs_fuse_main         # 本节点
for h in <fuse-client-1> <fuse-client-2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart hf3fs_fuse_main"
done
```

挂载点残留时先 `umount ${MOUNT_POINT}` 再 restart（报 `Transport endpoint is not connected` 就是这个原因）。

### 6. 验证配置生效

```bash
$ADMIN_CLI "get-config --node-type STORAGE -o /tmp/storage_verify.toml"
grep "sync_when_write" /tmp/storage_verify.toml
# 应输出: sync_when_write = false
```

**同步各节点本地配置**：两个 storage 节点 `/opt/3fs/etc/storage_main.toml` 也改成 `false`，否则下次重启会用旧文件启动（见 `config-mechanics.md` §1.5）。

---

## 三、性能对比

同口径：3 客户端 SSH 并行、USRBIO 引擎、`numjobs=32 bs=4M iodepth=8`。

### `sync_when_write=true`（ConfigVersion 15）

```
总带宽: 10068 MiB/s
fuse-client-1: 3341 MiB/s
meta:          3333 MiB/s
fuse-client-2: 3394 MiB/s
```

### `sync_when_write=false`（ConfigVersion 16）

```
总带宽: 12767 MiB/s (+26.8%)
fuse-client-1: 4077 MiB/s
meta:          4245 MiB/s
fuse-client-2: 4445 MiB/s
```

**每客户端分解要看**：三台都涨（+22%/+27%/+31%）说明是集群侧生效，不是某台客户端的偶然波动；若只有一台涨，先怀疑那台客户端的二进制/配置与其它不一致。

复现命令：

```bash
MODE=multi NODES="<fuse-client-1> <meta> <fuse-client-2>" bash scripts/fio_bench.sh seq-write
```

---

## 四、回退步骤

```bash
# 1) 用备份的 ConfigVersion 15 配置重新下发
$ADMIN_CLI "set-config --type STORAGE --file ${BACKUP_DIR}/STORAGE_config_v15_<时间戳>.toml \
  --desc 'sync_when_write=true (rollback)'"

# 2) 重启 storage 服务（全部节点）
read -rs SSHPASS; export SSHPASS
for h in <storage1> <storage2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart storage_main"
done

# 3) 重启 FUSE 客户端
systemctl restart hf3fs_fuse_main
for h in <fuse-client-1> <fuse-client-2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart hf3fs_fuse_main"
done

# 4) 回读确认 + 复测基线
$ADMIN_CLI "get-config --node-type STORAGE -o /tmp/storage_rollback.toml"
grep sync_when_write /tmp/storage_rollback.toml      # 应为 true
```

`scripts/set_config_param.sh` 每次执行都会打印这条回滚命令（含它自己生成的备份文件路径），照抄即可。

---

## 五、风险评估

**关闭 `sync_when_write` 的风险**：

- 掉电或 storage 进程崩溃时，未 fdatasync 的 WAL 数据可能丢失
- 可能导致元数据不一致，需要依赖 chain replication 的副本恢复机制

**适用场景**：

- 性能测试环境（可接受数据丢失）
- 有 UPS 保护的机房
- 多副本配置（2 副本或 3 副本提供冗余）

**不适用场景**：

- 生产环境且无其他数据保护机制
- 单副本配置

**收益上限**：这 +26.8% 建立在"FLUSH 本身不算快"的硬件上。若 NVMe 固件的 fdatasync 已经是 2.3 µs 量级，收益会显著小于 26.8% —— **先测裸盘 fdatasync 分位数再决定要不要承担这个风险**（`hw-and-firmware.md` §2.2）。

**作用范围别记错**：该开关只影响 commit 阶段的 KV 写；**update 阶段的 sync 是源码硬编码的**（`meta_store.rs:348-355` 的 `sync=true` 字面量），关掉开关不会消掉那部分开销。这也是"固件长尾集群上软件侧无化解空间"的原因。

---

## 六、后续方向

火焰图显示的主要热点（`sync_when_write=false` 之后）：

1. **CRC32C checksum: 31.0%** —— 配置侧无解（`chunk_checksum_type='NONE'` 会导致写全部失败，见 `config-mechanics.md` §3.4），只能改代码，归 skill `3fs-hotspot-code-optimization`
2. **XFS fallocate: 15.4%** —— 可尝试增大 `[server.targets.storage_target.meta_store] allocate_size`（256MB → 1~2GB）或增大 chunk size
3. **parquet/monitor: 7.0%** —— 可降低 `[common.monitor] collect_period`/`num_collectors` 采样频率（注意这会同时削弱观测能力，做 A/B 时要权衡）

**热点占比会随上一项优化而重新分布**：关掉 sync 之后 CRC 从 23.6% 那一层里"浮"到 31.0%，说明总时间变小了。**每轮改参后都要重采火焰图**，不要拿旧火焰图排下一轮的优先级。

---

## 七、可复用的动作序列（照抄清单）

```
[ ] 0. 前置: 瓶颈归因(软件配置面?) + 查 lever-ranking.md 两张表 + 高风险项已获用户确认
[ ] 1. 基线压测(同口径) + 记录 ConfigVersion + 二进制 md5 + 火焰图
[ ] 2. get-config 导出并备份(文件名带 ConfigVersion 与时间戳)
[ ] 3. 改一个变量(段落感知, 匹配数 != 1 就停手)
[ ] 4. set-config --desc '<改了什么>' → 记录新 ConfigVersion
[ ] 5. 回读 get-config 确认生效值 == 目标值
[ ] 6. 最小重启面(storage 全部节点 → FUSE 客户端; 挂载点残留先 umount)
[ ] 7. 同步各节点 /opt/3fs/etc/ 同名 toml
[ ] 8. 复测(同口径, ≥3 次) + 每客户端分解 + RDMA/延迟拆分佐证
[ ] 9. 台账: 带宽 / CPU-秒 / 压缩率 / 延迟分位数分开记, 附 ConfigVersion 与回滚命令
[ ] 10. 无收益或劣化 → 立即回滚, 并把结论写进"已证伪清单"
```
