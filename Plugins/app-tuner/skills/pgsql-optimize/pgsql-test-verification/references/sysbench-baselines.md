# sysbench 三负载基准与 A/B 协议

> 口径基线（源资料实测）：PostgreSQL 17.5 自编译、ARM aarch64 240 核、
> sysbench 1.0.20、64 表 × 1000 万行（~186GB）、扩展协议 + 服务端 prepared
> statements、pg_stat_statements 已加载；输出 TPS/p95/err 逐轮归档。

## 1. 数据准备（一次准备，多轮复用）

```bash
# 输出目录先创建再重定向（prepare.log 即在 ${BENCH_OUT} 下落盘，目录缺失时
# bash 重定向失败、prepare 证据丢失）——本节是 ${BENCH_OUT} 的首次使用
mkdir -p "${BENCH_OUT}" \
  || { echo "ERROR: 无法创建输出目录 ${BENCH_OUT}" >&2; exit 1; }
# prepare 阶段（只做一次；规模经变量注入，A/B 两轮共用同一份数据）；
# tee 会吞掉 sysbench 退出码（管道整体取 tee 的 0）——管道结束后**第一条语句**把
# 两项退出状态一次保存进数组（任何后续命令都会覆盖 PIPESTATUS），执行或归档
# 任一失败都停止（tee 失败=磁盘满/权限，基线证据已丢失，不能继续）
sysbench oltp_read_write --db-driver=pgsql \
  --pgsql-host="${PGHOST}" --pgsql-port="${PG_PORT}" \
  --pgsql-user="${PGUSER}" --pgsql-db="${DBNAME}" \
  --tables="${SYSBENCH_TABLES}" --table-size="${SYSBENCH_ROWS}" \
  --threads=64 prepare 2>&1 | tee "${BENCH_OUT}/prepare.log"
prep_st=("${PIPESTATUS[@]}")
if [ "${prep_st[0]}" -ne 0 ] || [ "${prep_st[1]}" -ne 0 ]; then
  echo "ERROR: prepare 失败（sysbench rc=${prep_st[0]}：连接/依赖/参数？；tee 归档 rc=${prep_st[1]}：磁盘满/权限？），见 ${BENCH_OUT}/prepare.log" >&2
  exit 1
fi
```

- 口令经 `PGPASSWORD` 环境变量注入（sysbench 从环境读，勿用 --pgsql-password
  留 shell 历史）。
- prepare 后做一次**计数对账**（`SELECT count(*) FROM sbtest1` … 每表）与
  `pgstattuple` 死元组抽查，记入基线身份——数据不同轮不可比。

## 2. 三负载运行（预热 + ≥3 轮）

```bash
# 轮次子目录（${BENCH_OUT} 已在 §1 创建；此处幂等补建轮次侧目录）
mkdir -p "${BENCH_OUT}/${BENCH_SIDE:-base}" \
  || { echo "ERROR: 无法创建输出目录 ${BENCH_OUT}/${BENCH_SIDE:-base}" >&2; exit 1; }
BENCH_THREADS="${BENCH_THREADS:-64}"     # 运行前校验正整数（空串/非数字会让 sysbench 报错或行为漂移）
BENCH_TIME="${BENCH_TIME:-60}"           # 同上
for v in BENCH_THREADS BENCH_TIME; do
  printf '%s' "${!v}" | grep -qE '^[1-9][0-9]*$' \
    || { echo "ERROR: ${v} 须为正整数（当前：${!v}）" >&2; exit 2; }
done
for t in oltp_read_only oltp_read_write oltp_write_only; do
  for r in 1 2 3 4; do                    # 首轮预热，结果入档但统计剔出
    log="${BENCH_OUT}/${BENCH_SIDE:-base}/${t}_r${r}.log"
    [ -e "$log" ] && { echo "ERROR: $log 已存在（重跑请换批次目录或先归档），拒绝覆盖" >&2; exit 3; }
    # 判定绑定 sysbench 退出码：日志非空≠成功（失败 stderr 也被 2>&1 收进日志，
    # 失败轮会冒充成功轮归档、且非空日志阻断同目录重跑）
    if ! sysbench "$t" --db-driver=pgsql \
      --pgsql-host="${PGHOST}" --pgsql-port="${PG_PORT}" \
      --pgsql-user="${PGUSER}" --pgsql-db="${DBNAME}" \
      --tables="${SYSBENCH_TABLES}" --table-size="${SYSBENCH_ROWS}" \
      --threads="${BENCH_THREADS}" --time="${BENCH_TIME}" --report-interval=10 \
      --percentile=95 run > "$log" 2>&1; then
      echo "ERROR: sysbench 本轮失败（见 $log：连接被拒/表不存在/磁盘满？）" >&2; exit 1
    fi
    # 退出码之外再断言汇总行：防「跑完但无结果」的静默异常轮混入基线
    grep -q "transactions:" "$log" \
      || { echo "ERROR: $log 无汇总行（transactions:），疑似异常轮" >&2; exit 1; }
    unset log
  done
done
```

- 三负载特征（判读用，口径见上；按 sysbench 1.0.20 默认 oltp_common.lua 脚本，
  未改任何 --*-hints/范围参数）：
  - `oltp_read_only`：每事务 **10 个点查 + 4 个范围查询**（simple/sum/order/
    distinct ranges 各 1，共 14 读；无写操作）；
  - `oltp_read_write`：上述 14 读 + **4 写**（1 索引 UPDATE + 1 非索引 UPDATE +
    1 DELETE + 1 INSERT；刷脏与 WAL 混合链路）；
  - `oltp_write_only`：每事务 **4 写**（1 索引 UPDATE + 1 非索引 UPDATE +
    1 DELETE + 1 INSERT；WAL/提交/锁自旋主导，无读查询）。
- 每轮独立 log（时间戳/轮号命名，不覆盖）；`--report-interval` 的分段 TPS 留
  稳态判据（末段与均值偏差 >10% 判窗口不稳，作废该轮）。

## 3. A/B 协议（同口径铁律）

1. **单一变量**：对照组/实验组只差目标变更（GUC、补丁、编译选项），其余
   configure/GUC/观测扩展开关/压测参数/threads/time 完全一致；
2. **同数据同基线同预热**：读写/纯写轮次会更新行并产生死元组，先跑完 A 直接换
   二进制跑 B 会把 B 的起始表/索引状态差异计入优化收益——两侧须从**同一份基线
   快照**恢复：prepare 完成并计数对账后先落基线（独占环境可用文件级/快照方式；
   共享环境用 pg_dump 逻辑备份），A 侧恢复→预热→测，B 侧恢复→同一预热步骤→测；
   换二进制仍按 build-deployment 的备份回滚流程逐次切换；
3. **交错执行**（可选加强）：ABABAB 排序抵消时间漂移（磁盘/环境温漂）；
4. **判定**：剔首轮后 ≥3 轮均值比较；收益 <5% 判噪声（不做结论）；负收益
   必须记录并回滚变更。

## 4. 结果归档模板

```
${BENCH_OUT}/
  identity.txt        # 版本 tag、configure 串、md5、GUC diff、sysbench 参数
  <load>_r<n>.log     # 逐轮原始输出（含 report-interval 分段）
  summary.txt         # 逐轮 TPS/p95/err + 均值/波动 + 判定（有效/噪声）
```

- summary 的每行带轮号与时间戳；判定行写明“收益 X%（剔首轮 3 轮均值，
  噪声阈值 5%）”——不带口径的百分比不得进入结论。
