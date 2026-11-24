# IO Optimization Playbook（实战经验）

> 来源：2026-11-19 在 openEuler 24.03 LTS-SP2 aarch64 真机（384 核 / 4 NUMA 节点 / 749 GB / 3×NVMe 2.9 TB / ext4 on multipath）上的端到端验证。数据均为实测，环境差异时以现场证据为准。

## 1. 预筛（detect_bottleneck.sh）实测行为

### 1.1 空闲基线

- 384 核机器空闲时：`mpstat_cpu_pct ≈ 1300`（聚合 core-percent，= 384 核 × ~3.4%）、`mpstat_idle_pct ≈ 96.4`（all 行平均）、`iowait 0`、`disk_util < 2%` → 正确输出 `no_active_bottleneck`
- 注意：`mpstat_cpu_pct` 是**全核聚合值**（可比 `cores * 85` 阈值），不是单核百分比；`mpstat_idle_pct` 是 all 行平均百分比。读证据时不要混淆两种口径

### 1.2 mpstat 列错位回归（已修复，重点看守）

- sysstat >= 12 在 `%steal` 后插入 `%guest`、`%gnice`，`%idle` 移到 $12；固定列号会把 `%guest` 当 idle（恒 0.00）、`%soft` 当 irq、`%steal` 当 soft
- 症状：`mpstat_idle_pct` 恒为 0.00 而 CPU 明明空闲
- 修复（三层全覆盖，检视 3）：从表头按列名动态定位——实时 `mpstat` 调用、`mpstat.txt` 存档解析（`parse_host_cpu`，供 NPU 馈送判定，曾丢 `%irq` 导致馈送瓶颈漏判）、`pidstat` 的 `%CPU` 列（原 `$(NF-6)` 实际指向 `%usr`，多词进程名时漂移更远）；并按 `CPU`/`UID` 列位置修正 AM/PM locale 的表头时间前缀偏移
- tier-A 用例 `cases/unit-prescreen-classify/` 用新旧两种表头、AM/PM locale、多词进程名与合成 NPU 存档回归看守

### 1.3 磁盘负载标定

- 单流 `dd bs=4M oflag=direct` 在 NVMe 上跑到 **5.4 GB/s 也只有 ~70% util**，不触发 `disk_util > 80` 阈值——单流顺序写不足以让现代 NVMe 饱和
- 3 路并行 direct-write 才把 `%util` 推到 **100.88** → 正确判定 `disk_bottleneck` / `disk_bandwidth`
- **结论：单流压不满 ≠ 设备无瓶颈；复现磁盘瓶颈要并行加压**。同时 `iowait` 在 direct IO + 快 NVMe 下恒为 0，不能作为"无磁盘压力"的证据；`%util` 与 `aqu-sz`/`await` 联合看

### 1.4 网络判定

- 脚本网络信号来自 `ss -tinp`（目标进程 socket 队列）与 `/proc/net/dev` 采样，**没有** `sar -n DEV` 带宽判定；带宽上限类证据需要人工补采
- 目标进程没有 socket 时输出 `socket_view_available_but_pid_not_found`（仅提示，不阻塞 `no_active_bottleneck`）

## 2. 磁盘 IO 专项（collect_io_evidence.sh）实测

饱和负载下的输出示例（3 路 dd，供字段口径参考）：

| 字段 | 实测值 | 说明 |
| --- | --- | --- |
| `nvme1n1.util_pct` | 94.56 | 挂载 /home 的 NVMe |
| `nvme1n1.wkB_per_sec` | ~6214848 | 单盘约 6 GB/s（字段为 kB/s 口径，÷1024 换算 MB/s） |
| `nvme1n1.w_await_ms` | 0.33 | direct 写延迟 |
| `nvme1n1.scheduler` | `[none] mq-deadline kyber bfq` | 方括号为当前值 |
| `nvme1n1.read_ahead_kb` | 4096 | 默认 4 MB |
| `nvme1n1.nr_requests` | 1023 | |
| `vm.dirty_ratio` | 60 | 该机已调过（默认 20） |
| `vm.swappiness` | 10 | 该机已调过（默认 60） |

注意事项：

- `devices[]` 只报活跃设备（`util > 0.01`），容器宿主机上上百个 idle nbd 会被自动过滤
- dm-*/multipath 设备无 `queue/scheduler`（输出 null），真实 scheduler 要看底层物理盘（如 nvme1n1）
- `mounts[]` 只含 `/dev/` 块设备挂载；容器宿主机的 `findmnt` 会返回数千个 nsfs 伪挂载，已过滤
- PSI（`/proc/pressure/io`）在该环境内核未启用，输出 null 并记 `psi_io_unavailable`——PSI 不是可依赖的信号，缺失时靠 iostat/vmstat

### 2.1 负载生成方法（复现磁盘瓶颈）

```bash
# fio 缺失时的替代：循环 dd + direct IO，N 路并行
for i in 1 2 3; do
  nohup bash -c 'while true; do dd if=/dev/zero of=/home/bale/io_load_$i.bin bs=4M count=4000 oflag=direct 2>/dev/null || break; done' &
done
# 清理：kill 循环 shell 的 PID 后 pkill -x dd（用 -x 精确匹配进程名，
# 不要 pkill -f 匹配文件名——会把包含该路径的 ssh 会话一起杀掉）
```

## 3. 内存专项（collect_memory_evidence.sh）实测

- 24 进程 memcpy 压力（每进程 2×192 MB buffer）下：
  - node1 内存占用 189/193 GB，其余 node 基本不变——**进程默认散布分配，node 级占用是定位内存压力落点的第一入口**
  - 系统 numastat 远端缺失率 2.46%（node3 最高 4.9%）——远端缺失率基线在个位数；>20% 才值得进入 NUMA 优化路线
  - 系统级 perf cache-miss 率仅从 2.20% 升到 2.71%：24/384 核的负载在**系统级平均**里被稀释，cache-miss 率要结合目标进程 perf 采样看，不能只看系统级
- swap 活动是容量判定的一等证据（`si/so` 每秒 KB）；该机 `si=0 so=0.6` 属噪声级

## 4. 常见坑

| 坑 | 症状 | 规避 |
| --- | --- | --- |
| mpstat 新表头列错位 | idle 恒 0.00 | 表头动态解析（已修复），旧环境升级 sysstat 后回归 |
| 单流 IO 压不满 NVMe | util ~70% 判不出瓶颈 | 并行加压复现 |
| iowait 在 direct IO 下恒 0 | 误判"无 IO 压力" | %util + aqu-sz + await 联合判断 |
| pkill -f 误杀自身会话 | ssh 命令含文件名被杀 | pkill -x 按进程名精确匹配 |
| nsfs 伪挂载淹没 mounts | 输出数千行 | 只取 /dev/ 开头的块设备挂载（已过滤） |
| PSI 未启用 | pressure 字段恒 null | 降级到 notes，不伪造 |
| PSI 字段固定 substr 偏移 | avg10=12.34 被读成 2.34、42.00 读成 2，1%~10% 判别区间被报成接近 0 且无任何提示 | 按 '=' 定位取值（index 解析），非法数值降级 null + notes；PSI_DIR 可注入合成文件回归（tier-A 用例看守，2026-11 检视修复） |
| bash 拼接 JSON 的字面 `\n` | 尾逗号/双反斜杠非法 JSON | 用 `$'\n'` 真实换行 + unittest json.load 校验（见 tier-A 用例） |
| vmstat 首行是开机以来均值 | 窗口均值被稀释：wa=15 被摊成 8（低于 10 阈值翻分类）、so=0.60 被摊成 2.80（历史换页冒充当前活动） | NR>3 跳过 since-boot 首行，与 iostat 第二段口径一致；DURATION=1 窗口为空时降级 null + `vmstat_since_boot_only` note（检视 6） |
| iostat 表头形态随 sysstat 版本变化（`Device:` vs `Device`） | 表头判定失配时 devices 恒空且无提示，"无活跃设备"与"没解析到"不可区分 | 前缀匹配两种形态；表头不可解析降级 `iostat_header_unparsed` note（检视 8/9）；mock 必须复刻真实工具形态而非复刻实现 |
