---
name: oceanbase-hotspot-analysis
description: >
  OceanBase 热点分析与 profiling：按场景（write_only、read_only、point_select、insert、
  update、交易类业务模型）采集 perf/火焰图、on/off-CPU、Topdown/PMU、SPE topdelay、
  htop/pidstat/mpstat、obproxy 火焰图、GV$SYSSTAT 与线程状态视图、sockmap/eBPF；
  结合 sql_nio/worker/ApplySrv/ReplaySrv/冻结合并线程做归因，覆盖排序与字符比较、
  微块解码、DAS 归并、内存分配、锁与 KV Cache、redo/转储、跨 NUMA/跨 P、L3 Bound
  等机制，并给出瓶颈迁移与优化方向。用于回答“慢在哪、为什么、证据是什么”；
  不修改代码/配置，不执行部署与压测（分别交给 oceanbase-build-deployment /
  oceanbase-test-verification / oceanbase-performance-optimization）。
  触发词：OceanBase 热点、OB profiling、OB 火焰图、perf、off-CPU、topdown、PMU、
  L3 Bound、ApplySrv、sql_nio、GV$SYSSTAT、跨 NUMA、跨 P、P99 高、CPU 利用率低。
license: MulanPSL-2.0
compatibility: OceanBase CE 4.3.5 源码树 4.3.5_CE_BP5_HF2；perf/FlameGraph、bcc offcputime、DevKit/ALL_Linux_PMU、SPE、sockmap；鲲鹏 950/920B、AMD 9T34 实测
metadata:
  skill_type: tool_assisted
  category: diagnosis
  domain: relational_database
  version: "1.0.0"
---
# OceanBase 热点分析

## 简介

目标不是“贴一张火焰图”，而是给出一条可复核的因果链：

```
场景/负载 → 瓶颈进程/线程 → 热点函数/阶段 → 机制（锁/RPC/访存/IO/算法）→ 证据 → 优化方向
```

本 skill 覆盖四层证据：

1. **系统/线程层**：CPU 利用率、线程分布、off-CPU 等待、上下文切换、IO、跨 NUMA。
2. **函数层**：perf/火焰图的 self/children 热点、调用路径聚合。
3. **微架构/阶段层**：Topdown（Frontend/Backend/Core/Memory/L3 Bound）、IPC、cache miss、
MPKI、SPE topdelay 指令时延。
4. **数据库内部层**：`GV$SYSSTAT`（rpc/io/memstore）、`GV$OB_SQL_AUDIT`、线程状态视图。

## 何时触发

- 已有**可采信基线**（同口径协议见 `oceanbase-test-verification`）前提下的归因诉求：TPS/QPS 上不去、CPU 利用率异常（过高/过低）、P99 长尾、场景切换后性能变化——尚未建立可采信基线时，先回 `oceanbase-test-verification` 建基线。
- 要确认瓶颈在 observer、obproxy、sysbench，还是在 IO/网络/NUMA。
- 优化前后要解释“收益来自哪里”，或负收益要定位原因。
- 需要为某个场景建立热点基线（write_only/read_only/point_select/insert/update）。
- 怀疑 ApplySrv、sql_nio、worker、锁、L3、跨 P、redo/转储、obproxy 转发。

**不适用**：部署/编译 → `oceanbase-build-deployment`；压测口径与 A/B 判定 →
`oceanbase-test-verification`；改代码/调参 → `oceanbase-performance-optimization`；
OS/BIOS/网卡/绑核的通用方法 → `kpbot-app-tuner` 的对应 subskill（OB 专属绑核见
`oceanbase-performance-optimization`）。

## 采集前先固定场景

没有固定场景的热点没有意义。记录：

- 场景与 lua（write_only/read_only/point_select/insert/update 或业务模型），是否经 obproxy；
- 线程/VU、时长、sysbench/obproxy 绑核；
- 部署形态、规格、租户 Unit、`primary_zone`；
- 数据集与表规模；
- OB 版本、observer/obproxy md5、编译参数；
- 绑核与 NUMA（是否跨 P、是否 cgroup）。

同时记录 TPS/QPS、成功率、avg/P95/P99、observer/obproxy/sysbench CPU，作为热点解释的对照。

## 采集速查

```
# 1) 进程/线程（按 PID，不要整机 grep）
ps -Lo pid,tid,comm,psr,pcpu -p "${OBS_PID}" | head -40
top -H -p "${OBS_PID}"

# 2) perf 采样（默认按 PID；必须 -g 才能还原父调用链；窗口覆盖稳态）
perf record -p "${OBS_PID}" -F 99 -g -- sleep 30
perf report --stdio --sort dso,sym | head -60
# 整核采集（显式选择，含核上所有任务；仅目标固定核且需对比同核进程时用，产物标明范围）：
perf record -C 0-31 -F 99 -g -- sleep 30
perf top -C 0-31 -- sleep 10

# 3) 火焰图（产物名带采集范围标签，与脚本一致）
perf script | FlameGraph/stackcollapse-perf.pl | FlameGraph/flamegraph.pl > "observer_pid${OBS_PID}.svg"

# 4) off-CPU（锁/IO 等待）
/usr/share/bcc/tools/offcputime -p "${OBS_PID}" 30 > observer_offcpu.txt

# 5) Topdown / PMU（平台工具；代表核 8-20s）
bash ALL_Linux_PMU.sh <outdir>
# 或 DevKit topdown（如可用）

# 6) htop/pidstat/mpstat
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "${OBS_PID},${PROXY_PID},${SYSBENCH_PID}" 1 30 > pidstat.txt

# 7) OB 内部指标
obclient -h <host> -P ${OB_PORT} -uroot@sys -Doceanbase -A -e \
  "SELECT /*+read_consistency(weak)*/ NAME,VALUE FROM GV\$SYSSTAT WHERE STAT_ID IN
   (10000,10001,10002,10003,10005,10006,140002,140003,40006,40007,40008,40009,
    40010,40011,40012,50000,50001,50008,50009,60000,60001,60002,60003,60004,
    60005,60019,60020,60021,60022,60023,60024,80057) AND CON_ID > 1000;" \
  > sysstat_start.txt   # 稳态窗口起点；终点再采一份 sysstat_end.txt，按 STAT_ID 求差

# 8) 线程状态（列名以版本为准；示例）
obclient ... -e "SELECT SVR_IP,SVR_PORT,TENANT_ID,TID,TNAME,STATUS,LATCH_WAIT,LATCH_HOLD,CGROUP_PATH FROM GV\$OB_THREAD;" > threads.txt
```

采集纪律：

- 先预热再采样，窗口覆盖稳态；采样期间不要改配置/数据。
- 多实例时分别采 observer/obproxy；记录采的是哪个 PID/核。
- `perf` 默认按 `OBS_PID` 采样（多实例共用核或目标不在指定核时不混采、不漏采）；仅在显式 `PERF_SCOPE=cpu` 时按 `OBS_CPUS` 整核采集，并在产物名/meta/status 单独标明范围。
- `perf record` 必须带 `-g`（或显式 `--call-graph dwarf`）才能用 `perf script` 还原父调用链；否则火焰图无法做报告要求的 children 分析。`--call-graph dwarf` 需要调试信息（`-g` 且不 strip）；`[unknown]` 过高先补符号。
- `scripts/collect_profile.sh` 拒绝非空 `--out` 目录，避免把上次未运行采集器的旧产物计作本轮成功；perf/offcpu/mpstat/pidstat/iostat 并发启动，`timing.txt` 记录各自起止时间，保证同一窗口对照。
- sockmap/eBPF 属状态变更实验：采集流程只检查当前状态；启用/卸载放单独授权 A/B 流程，并在结束后核验恢复（见 `references/profiling-playbook.md` §6）。
- 火焰图看 self 与 children 两种视图，避免把“等待”误判为“计算”。
- off-CPU 看**累计离核时长**，不是 `sched_switch` 次数占比。

## 判读要点（口径下沉 references）

| 场景 | 主要瓶颈位置 | 典型热点 |
| --- | --- | --- |
| write_only | observer + IO | redo/clog、冻结合并、ApplySrv |
| read_only | observer | 排序/字符比较、微块解码、DAS、L3 Bound |
| point_select | observer | MemTable、KV Cache、latch |
| insert | observer | ApplySrv/序列化、redo、跨 P |
| update | observer | MVCC 版本链、锁、gettimeofday |
| 业务（交易/账务） | observer + IO | 写放大、redo、冻结；与 sysbench 不可混比 |

判读纪律：先分进程（observer/obproxy/sysbench 谁先饱和）→ 再分线程（`sql_nio`/
worker/ApplySrv/ReplaySrv/冻结合并）→ 再分机制（锁/内存/存储/算法/写入/访存）→
用对照验证。`perf` 必须带 `-g`，火焰图看 self 与 children；`off-CPU` 看累计离核时长
而非 `sched_switch` 次数；`GV$SYSSTAT` 必须快照差分，时点值不等于窗口值；
`L3 Bound`/跨 NUMA 的结论要有对照或 PMU 证据；`ApplySrv` 饱和时加 worker 无效。
事实（火焰图占比）与推断（“因 L3 Bound 所以慢”）分开写。

完整场景热点表、迁移规律与常见误判见 `references/scenario-hotspots.md` 和
`references/profiling-playbook.md` §11；原实测百分比已脱敏，需具体数字须重采。

## 输出契约

报告至少含：场景与身份（版本/md5/绑核/Unit/primary_zone）、性能对照（TPS/QPS、
成功率、avg/P95/P99、三类 CPU）、线程/阶段分解、Top 热点表（self%/children%/
函数/线程/机制/证据）、微架构与 `GV$SYSSTAT` 指标、归因链（进程/线程 → 机制 →
为什么）与优化方向（含验证方式、假设/已验证标注）。完整模板见
`references/profiling-playbook.md` §12。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/profiling-playbook.md` | 采集命令、符号、火焰图、off-CPU、Topdown/PMU、SPE、sockmap 状态检查与授权 A/B、线程/指标 SQL、窗口与注意事项 |
| `references/scenario-hotspots.md` | 五场景热点表、热点机制与实测差异、950/920B/AMD 差异、瓶颈迁移、obproxy 对照 |
| `references/thread-and-queue-model.md` | 线程命名与职责、请求队列、sql_nio→worker、Apply/Replay、冻结合并、如何用线程判断瓶颈 |
| `references/stats-topdown.md` | GV$SYSSTAT 指标解释、Topdown 指标树、IPC/MPKI、如何区分计算/访存/IO |
| `scripts/collect_profile.sh` | 只读采集：默认 dry-run 打印计划；`--execute` 并发采 perf（默认按 PID）/offcpu/pidstat/mpstat/iostat，窗口起止各采一份 `sysstat`（`sysstat_start.txt`/`sysstat_end.txt`，供差分）并采线程/审计视图，`timing.txt`/`status.txt` 记起止与各通道 ok/missing，拒绝非空 `--out`，无数据 exit 非零 |

`scripts/collect_profile.sh` 变量表（默认值与脚本一致）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OBS_PID` | `pgrep -x observer`（多实例须显式指定） | 目标 observer PID；perf 默认按它采样 |
| `PERF_SCOPE` | `pid` | `pid` 按 OBS_PID；`cpu` 整核 `OBS_CPUS`，产物名/meta/status 标注 `perf_label` |
| `OBS_CPUS` | `0-31` | 仅 `PERF_SCOPE=cpu` 时用于 `perf -C`；默认不按核采集 |
| `SAMPLE_SECS` | `30` | 采样窗口秒数（1-600），各并发采集器共用 |
| `OUT_DIR` | `./ob_profile_<时间戳>` | 必须为空目录或不存在；非空直接拒绝，避免复用旧产物 |
| `PROXY_PID` / `SYSBENCH_PID` | 空 | pidstat 附带目标 PID |
| `OB_HOST` / `OB_PORT` | `127.0.0.1` / `2881` | obclient 连接地址/端口；默认取官方 `mysql_port` 2881（2882 是 `rpc_port`，直连打不到 SQL 服务）；实测部署若不同以 `install.cfg` 为准并显式设置，端口 1-65535 |
| `OB_USER` / `OB_DB` | `root@sys` / `oceanbase` | obclient 用户/库 |
| `OB_PASSWORD` | 空 | 非空时写入 mktemp 临时 defaults 文件并 trap 清理，不进 argv |
| `PMU_SCRIPT` | 空 | Topdown/PMU 脚本路径 |
| `FLAMEGRAPH_DIR` | 空 | FlameGraph 目录；提供时生成带范围标签的 `observer_<label>.svg` |
| `EXECUTE` | `0` | 由 `--execute` 置 1；dry-run 不落盘 |
