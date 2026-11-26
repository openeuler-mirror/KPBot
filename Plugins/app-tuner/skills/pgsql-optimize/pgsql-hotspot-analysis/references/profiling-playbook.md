# profiling 手册（perf / topdown / 等待事件 / off-CPU）

> 只读采集。所有命令按目标环境替换路径与 PID；百分比判读须带口径行。

## 1. on-CPU 火焰图

```bash
# 多实例逗号口径：pgrep 输出空格分隔，perf -p 要逗号
PIDS=$(pgrep -f 'postgres: postgres' | tr '\n' ',' | sed 's/,$//')
perf record -F 999 -g -p "$PIDS" -- sleep 20
perf script | ${FLAMEGRAPH_DIR}/stackcollapse-perf.pl \
           | ${FLAMEGRAPH_DIR}/flamegraph.pl > pgsql_oncpu.svg
```

- `-F 999` 避开采样谐振；窗口 20-60s 覆盖稳态（重查询按需加长）；
- 生成二进制时若为 LTO 构建：帧名带 `.lto_priv`、消息处理（exec_*）内联进
  PostgresMain、顶层 `GetCommandTagEnum` 是归因伪影——顶层帧先归到主循环再下钻；
- 合并视角：`secure_read/tcp_recvmsg` 合并看“网络层”、`XLog*` 合并看“WAL”、
  `LWLock*` 合并看“锁机制层”——只看顶层帧容易误归因。

## 2. Topdown（ARM PMU）

```bash
# 代表核采样（全核会互相稀释；选压测主力核）
perf stat -C 8-15 -e cycles,instructions,branches,branch-misses,cache-misses -- sleep 10
# ARM topdown 指标名随平台/内核版本变化：用 perf list 查 topdown 前缀取本机可用组
perf stat -C 8-15 --topdown -- sleep 10 2>/dev/null || \
  perf stat -C 8-15 -e topdown-retiring,topdown-bad-spec,topdown-frontend-bound,topdown-backend-bound -- sleep 10
```

判读锚点（写负载实测坍塌形态，口径：PG 17.5、sysbench wo、ARM 240 核）：

| 指标 | 实测值 | 含义 |
|------|--------|------|
| IPC | 0.233（ro 0.731） | 周期浪费在等待，非计算 |
| Retiring | 2.9% | 有效指令占比极低 |
| 原子/屏障串行化 | 33.2% | 锁自旋的直接微架构证据 |
| L3_Bound | ~50% | 锁字/缓冲头/ProcArray 缓存行跨核乒乓 |

## 3. 等待事件采样（SQL 侧，锁等待的直接证据）

```sql
-- 快照式（空转库采不到属正常）
SELECT wait_event_type, wait_event, state, count(*)
FROM pg_stat_activity WHERE state='active' AND pid <> pg_backend_pid()
GROUP BY 1,2,3 ORDER BY 4 DESC;
```

```bash
# 短窗高频采样：**客户端侧单连接只读循环**（对被测库零写入——不建表、不 INSERT）。
# 单连接纪律：所有样本共用一条 psql 常驻会话——每样本新起 psql 会引入
# ~6000 次 TCP+SCRAM+fork backend（60s×10ms），观测者效应恰放大本域竞争画像；
# 本轮独立文件（时间戳命名、轮前不残留），同目录重跑不跨轮混合；
# 窗口估算须计入 psql 往返耗时（实际窗口 ≈ 6000×(0.01+单样本往返)）；
# 查询内 datname = current_database() 限定目标库：跨库会话不混入
STAMP="$(date +%Y%m%d_%H%M%S)"; OUT="wait_sampling_${STAMP}.txt"
( for i in $(seq 1 6000); do   # 6000 样本目标，按窗口调整
    printf '%s\n' \
      "SELECT now(), coalesce(wait_event_type,'-'), coalesce(wait_event,'-'), count(*) \
       FROM pg_stat_activity \
       WHERE state='active' AND pid <> pg_backend_pid() \
         AND datname = current_database() \
       GROUP BY 2,3;"
    sleep 0.01
  done ) | psql -w -h "${PGHOST}" -p "${PG_PORT}" -U "${PGUSER}" -d "${DBNAME}" \
            -X -qAt > "${OUT}"
# 采样后先校验再判读（缺终止符/连接失败时输出为空，空文件聚合=「无等待」假结论）：
#   [ -s "${OUT}" ] 或 die——输出须非空；行数与样本数同量级（每样本一组行），
#   明显少于样本数说明部分语句未执行（psql 报错在 stderr，退出码可能仍为 0）
[ -s "${OUT}" ] || { echo "ERROR: 采样输出为空（连接失败/语句未执行？）: ${OUT}" >&2; exit 1; }
```

分析：按本轮 `wait_sampling_${STAMP}.txt` 聚合（shell/awk 即可，count 之和=样本数；
now() 列仅作窗口核对），与下述快照式同判读：

```bash
# 本地文件聚合（对比优化前后）：按 wait_event_type/wait_event 汇总样本数与占比
awk -F'|' '{gsub(/ /,"",$2); gsub(/ /,"",$3); k=$2"|"$3; s[k]+=$4; n+=1}
           END {for (i in s) printf "%s %s\n", s[i], i}' "wait_sampling_${STAMP}.txt" | sort -rn
```

- 事件→机制对应（详见 `references/contention-mechanisms.md`）：
  `LWLock/BufferMapping`＝BufMappingLock 分区、`LWLock/WALWrite`/`WALSync`＝
  提交刷盘、`Lock`＝重量级锁（Lock Hash Table）、`BufferPin`＝pin 等待、
  `IO/DataFileRead|Write`＝buffer I/O。
- 可见性前提：观察者角色非超级用户/pg_read_all_stats 时跨角色会话脱敏（详见
  `../../pgsql-test-verification/references/observation-extensions.md` §4）。

## 4. off-CPU（锁等待的时间维度）

```bash
# bcc 可用时首选（按 PID 累计离核时长，单位 us）
/usr/share/bcc/tools/offcputime -p "$(pgrep -f 'postgres: postgres' | head -1 | tr '\n' ' ')" 20 > pgsql_offcpu.txt
# 只有 perf 时：sched_switch 次数≠等待时长（只是切换频次），判累计阻塞须配对时间戳
perf record -e sched:sched_switch -p "<单PID>" -g -- sleep 20
```

- offcputime 的栈=“睡眠原因链”（锁/IO/信号），与 on-CPU 栈互补；
- **阻塞睡眠不进 on-CPU 栈；忙自旋在栈里**：锁等待分两种形态——睡眠等待
  （off-CPU 时间兑现，on-CPU 火焰图看不到）与忙自旋/CAS 轮询（进程仍在 CPU 上，
  **可见于 on-CPU 火焰图**，如 LWLockWaitListLock/WaitXLogInsertionsToFinish 帧——
  与三负载画像里这些帧高占比一致）。注意 WaitXLogInsertionsToFinish 的帧高**不能
  直接读作忙自旋**：PG 17.5 起它在前序插入未完成时可经 LWLockWaitForVar 睡眠
  （睡眠时间不占 CPU，帧高可能是睡眠路径的调用方被采样）；同窗口 WALWrite/WALSync
  等待事件高则部分时长是等刷盘——归因前先分清进度检查、阻塞等待与刷盘三种形态。
  分区扩容类锁优化的收益在等待采样/offcputime 里最直接；“火焰图正常但吞吐差”
  优先查睡眠类等待；“火焰图里锁帧高”对应自旋类削减（先排拆睡眠路径）。勿用
  一条规则概括两种形态。

## 5. 采集窗口纪律

- 先预热（首轮数据入档但统计剔出）再采，采样期间不改配置；
- `env.txt` 记录：版本 tag、configure 串（含 LTO 与否）、GUC 关键项、负载参数、
  窗口与采样率——证据包可复现的第一要素；
- 同窗口并行采火焰图/pidstat/mpstat（`pidstat -p` 多 PID 逗号、与 perf 同窗）。
