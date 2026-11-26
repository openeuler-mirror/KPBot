# 杠杆收益排序 与 无效/反效果/已证伪清单

> **何时读我**：准备动任何 3FS 参数之前先读本文两张表 —— 第一张告诉你"该动哪个杠杆、值多少收益、风险多大"，第二张告诉你"哪些手段已经试过且无效/反效果/被证伪"，避免重复踩坑和重复归因错误。

数据来源：两套集群的 A/B 实测。A（3 storage，顺序写 8~10.5 GiB/s）与 B（2 storage，顺序写 3 GB/s），鲲鹏 920 / openEuler 24.03 LTS SP3 / 100Gb/s InfiniBand / NVMe / XFS。

文中节点用角色名指代（meta / storage1 / storage2 / storage3 / fuse-client-1 / fuse-client-2），示例 IP 见 `SKILL.md` 的节点约定表。

---

## 一、杠杆收益排序总表（先看这张表）

按实测收益量级排序。**部署侧杠杆普遍比代码侧大一个数量级** —— 同期代码侧替换 storage 的 crc32c 实现只有 CPU-秒 -6.1%、带宽 0%，且该实现后来因在鲲鹏 920 上段错误被整体废弃（Rust 侧 crc32c 优化已放弃，只保留 C++/folly 侧）。

| # | 杠杆 | 实测收益 | 风险 | 类型 |
|---|---|---|---|---|
| 1 | 客户端 `enableChecksum=false` | **7.8x**（1290 MiB/s → 9.79 GiB/s，同一二进制 md5） | 高：失去端到端数据校验 | 配置 |
| 2 | storage `sync_when_write=false` | **+26.8%**（10068 → 12767 MiB/s，3 客户端） | 高：掉电丢失未 fdatasync 的 WAL | 配置 |
| 3 | 副本数 3 → 2（写路径 3 跳 → 2 跳） | **+28%**（512m 口径，9681 → 12380 MiB/s） | 高：容错能力下降 | 拓扑 |
| 4 | FUSE 启动补 `--cfg` 参数 | **4x**（两台客户端曾只有第三台的 1/4） | 无：这是修缺陷 | 部署 |
| 5 | 客户端 `traffic_control.write` (32,8) → (256,64) | 785 → **1198 MiB/s（+53%）** | 中：512 会触发 `Bad file descriptor` | 配置 |
| 6 | 客户端 `num_bg_threads` 2 → 6 | 461 → **815 MiB/s（+77%）** | 低 | 配置 |
| 7 | `force_use_tcp=true` → `false` | 400 MB/s（4% 线速）→ RDMA 全速 | 无：这是修缺陷 | 配置 |
| 8 | NVMe 固件 8312 → 1059 | 预期消除 4.6x fdatasync 差距（**待验证**） | 中：需停机刷固件 | 硬件 |
| 9 | meta 节点 `CPUPrefetchConfig` Disabled → Enabled | **未实测** | 低 | BIOS |

**执行顺序建议**：先修缺陷类（4、7）→ 再查硬件类（8，5 秒钟的 `nvme id-ctrl` 检查）→ 再做低风险配置（5、6）→ 高风险配置（1、2、3）必须先与用户确认数据安全性要求。

**每项杠杆的落地细节**：

| 杠杆 | 详见 |
|---|---|
| 1 `enableChecksum` | `config-mechanics.md` §checksum 相关配置 |
| 2 `sync_when_write` | `config-mechanics.md` §sync_when_write + 完整闭环案例 `case-sync-when-write.md` |
| 3 副本数 / 写路径跳数 | `topology-and-placement.md` |
| 4 FUSE `--cfg`、二进制一致性 | 本文 §8.4（部署缺陷类）；部署与安装归 skill `3fs-deployment-installation` |
| 5 `traffic_control.write` | `config-mechanics.md` |
| 6 `num_bg_threads` | `config-mechanics.md`（注意需 4 处同改） |
| 7 `force_use_tcp` 与 NIC 选择 | `config-mechanics.md`（含 RDMA 计数器排查命令） |
| 8 NVMe 固件 fdatasync 长尾 | `hw-and-firmware.md` |
| 9 BIOS `CPUPrefetchConfig` | `hw-and-firmware.md`（Redfish 是唯一有效读取途径） |

---

## 二、无效 / 反效果 / 已证伪清单

**动手前先对照本表。** 表内每一项都是已经付出过实测成本的结论。

### 2.1 配置类

| 手段 | 结果 | 备注 |
|---|---|---|
| `chunk_checksum_type='NONE'` | **写全部失败** | 见 `config-mechanics.md`，不是可用开关 |
| `traffic_control.write max_concurrent_requests=512` | **FAIL：`Bad file descriptor`** | (256,64) 是上限 |
| `iodepth_batch_complete_min/max` 8 → 1 | 1447 → 1383 MiB/s，**无明显差别** | |
| `num_event_loop` / `max_connections` 增大 | **无变化** | |
| `rdma_listen_ethernet` true→false→true | **无可测影响** | |
| 单纯增大 fio `numjobs` | **无收益** | 瓶颈在 `batchWrite` 内部信号量：`waiting_time` 572 ms vs `server_latency` 4 ms（见 `observability.md` 延迟拆分） |
| `numjobs ≥ 128` | **劣化** | |

### 2.2 代码类（fio USRBIO 引擎）

`hf3fs_usrbio_commit()` 改异步（把 `hf3fs_wait_for_ios()` 移到 `getevents()`，新增 `in_flight`/`submitted_io_us`/`event_io_us` 映射）→ **-4%**。

**根因**：等待只是从 `commit()` 平移到 `getevents()`，每 job 仍只有一个 USRBIO batch in-flight，因为 `queue()` 里有硬窗口：

```cpp
if (sd->in_flight + sd->queued == td->o.iodepth) { return FIO_Q_BUSY; }
```

默认参数 `numjobs=32 iodepth=8 bs=4M iodepth_batch_submit=8 iodepth_batch_complete_min/max=8` 下的实际形态是：queue 8 个 → commit 提交 8 个 → in_flight 满、queue 返回 BUSY → getevents 等 8 个完成 → 下一批。**并发形态没变**，新增的 CQE index 映射与状态维护成了纯开销。

**为什么 FUSE 路径不同**：USRBIO 引擎里 fio `iodepth` 就是每 job 对 IOR ring 的硬窗口；而 FUSE/libaio 路径中 fio `iodepth` 只是进入内核/FUSE 的入口窗口，后面还有 daemon 自己的 `max_background`、`io_jobq_size`、worker/coro 队列与异步调度，能在 fio job 等待时继续供给 storage。

> 代码级优化（含 crc32c、fdatasync 深度根因分析）不在本 skill 范围，归 skill `3fs-hotspot-code-optimization`。

### 2.3 拓扑类

| 手段 | 结果 |
|---|---|
| 2 storage 下 HEAD 均分（chain table 3） | **41 MiB/s**，双向 RDMA 转发不稳定，已回退 |
| 3 storage 下 HEAD 分散（chain table 1，早期） | 性能下降，已回退 |
| 3 storage 下 rotate 到 8/8/8（chain table 4） | 中性（9559 → 9615 MiB/s），但每节点写入量均衡了 |

详见 `topology-and-placement.md`。

### 2.4 部署缺陷类（修了就是数倍收益）

| 缺陷 | 症状 | 修复 |
|---|---|---|
| FUSE `ExecStart` 缺 `--cfg` | fuse-client-2 与 meta 的写带宽只有 fuse-client-1 的 **1/4**（单客户端 1871 MiB/s；多客户端 fuse-client-2=2041 vs fuse-client-1=5055） | `ExecStart=/opt/3fs/bin/hf3fs_fuse_main --cfg /opt/3fs/etc/hf3fs_fuse_main.toml --launcher_cfg /opt/3fs/etc/hf3fs_fuse_main_launcher.toml` |
| 各节点二进制不一致 | 部分客户端只有 1.x GB/s | 从基准客户端复制并 **md5 校验**；保留构建备份目录与 `/opt/3fs/bin_backup_<ver>` |
| `force_use_tcp=true` | 400 MB/s（4% 线速） | 见 `config-mechanics.md` §force_use_tcp 与 NIC 选择 |
| 时钟偏差 34 秒 | target 集体 OFFLINE、rotate 失败 | 见 `topology-and-placement.md` §时钟同步 |

**调参前先排除这一类**：缺陷未修时测出的"基线"没有意义，任何参数收益都会被缺陷噪声淹没。

### 2.5 归因错误（方法论教训）

**① 把硬件问题当算法问题**：B 集群 3 GB/s 最初归因为 `crc32c::hw_aarch64` CPU 成本 + `copy_on_write` 全 chunk CRC 重算（声称 32.5~64x 开销），据此把 B 全部 target 用 `only_chunk_engine=false` 重建。**重建后仍 3 GB/s，整个重建白做。** ClickHouse A/B 反证：B 的 COW 比例（31.88%）比 A（100%）**更低**，两边 `checksum_recalculate` 都是 0。真因是 NVMe 固件（见 `hw-and-firmware.md`）。

**教训**：火焰图占比高 ≠ 它是原因。B 的 CRC 占比高是因为**其它环节更慢导致 CRC 在总时间里相对占比上升**。跨集群对比必须先排除硬件/固件差异。

**② 把配置收益记到代码优化头上**：部署"优化版"二进制后测得 2 GB/s → 10 GB/s，初判为代码收益 5x。事后发现部署的二进制 md5 `96dba758…` 与本地构建 `b49406af…` 不一致、含 **0 个加速符号**；真实原因是同期把 `enableChecksum` 改成了 false。证明方法是找到 md5 **完全相同**的二进制在两种配置下的表现（一台 true=1290 MiB/s vs 另一台 false=9.79 GiB/s）。

**教训**：A/B 两腿必须核对二进制 md5/sha256 并快照配置面（含 ConfigVersion）。一个改动同期发生多件事时，任何"显著收益"都先怀疑是另一件事造成的。

**③ 峰值当均值**：见 `observability.md` §RDMA 带宽监控。

**④ ClickHouse 3 小时窗口含多次测试与空闲期** → "总字节 / 3 小时" ≠ 测试吞吐。稳态要用逐秒时间线（A 10.5~10.9 GiB/s vs B 3.2~3.6 GiB/s）。

**⑤ fio 版本不一致污染对比**：A 用自编译 `fio-3.42-39-g5e65`（匹配 `hf3fs_usrbio.so` 的 ioops v39），B 用系统 fio 3.34，fallocate/laying-out 行为不同，污染"新文件 vs 覆盖写"对比。**该场景下只有 python append 测试可信。**

**⑥ B 集群 storage2 "快" 的假象**：259 MiB/s fdatasync 那次是**覆盖已存在文件**（两次跑之间没删），而 storage1 是新文件 —— 一度指向文件系统层原因。后续实验（预分配后覆盖仍慢，p99 1.4~1.6 ms）推翻了 XFS 分配路径理论。

**⑦ 服务状态误记**：用户认为 storage3 已停机，`systemctl status storage_main` 显示 **active (running) since Jul 3** —— 从未停过。**拓扑结论前先核实服务实际状态。**

### 2.6 环境类陷阱

| 陷阱 | 处理 |
|---|---|
| SSH 密码正确但认证失败 | `known_hosts` 里旧 RSA host key 禁用了密码认证 → `-o UserKnownHostsFile=/dev/null`；host key 变化 + hostname 变成 `localhost.localdomain` 说明机器刚重装 |
| fio `$jobnum` 被 shell 展开 | `--filename_format="seq-write.$jobnum.0"` 用双引号时 32 个 job 全写同一个文件，产出 556 MiB/s / io=4096MiB 的假结果 → **必须用单引号**（`scripts/fio_bench.sh` 已按数组传参规避） |
| fio USRBIO 报表假象 | per-job 行 `bw (MiB/s): min=1897,max=2049,avg=2045.64` 而汇总行 `WRITE: bw=1428MiB/s` → **以汇总行为准**，avg 行是 artifact |
| 读测试预写入尺寸不匹配 | 预写入大小必须与读测试 `--size` 一致（均 128M），否则结果无效（`scripts/fio_bench.sh` 的预填充会校验文件尺寸） |
| profiling 压负载 | 三路并发 DWARF 采集把 fio 从 1462 拖到 556 MiB/s → **带宽数字必须用不带 profiling 的干净跑** |
| 旧文档硬件描述错误 | 旧文档写 ConnectX-6 / 8×3.2TB NVMe / openEuler 22.03 SP4 / 鲲鹏 920-7260；实测为 **ConnectX-5（MT27800, part 4119, fw 16.31.1014）/ 每 storage 4×3.2TB / openEuler 24.03 SP3 / 鲲鹏 920-4826** → **以 `ibv_devinfo`/`lspci`/`nvme id-ctrl` 实测为准，不要信旧文档** |
| 节点配置看本地缓存 | 各节点 `/opt/3fs/etc/*.toml` 可能与运行时不一致 → **一律用 `get-config` 实际导出为准**（见 `config-mechanics.md`） |
