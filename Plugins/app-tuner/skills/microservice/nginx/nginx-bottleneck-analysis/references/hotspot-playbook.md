# Nginx 热点分析 Playbook（证据采集与精调执行细节）

> 本文件是 `nginx-bottleneck-analysis` SKILL.md 的执行细节层：判定树与路由见 SKILL.md。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

## 热点证据采集（命令自包含）

当分层调优仍不达标时，用 perf 火焰图 + mpstat + 网卡统计定位：

```bash
# 1. perf 采样（30s @99Hz，覆盖 worker 核）
# perf record 需要权限时优先 CAP_PERFMON / perf_event_paranoid，不默认整链 root
perf record -F 99 -C $CORES -a -g -o perf.data -- sleep 30

# 2. 生成火焰图（FlameGraph 固定到已审阅 commit；新建和复用目录都核对）
FLAMEGRAPH_COMMIT=41fee1f99f9276008b7cd112fca19dc3ea84ac32
[ -d FlameGraph ] || git clone https://github.com/brendangregg/FlameGraph.git
if [ "$(git -C FlameGraph rev-parse HEAD 2>/dev/null)" != "$FLAMEGRAPH_COMMIT" ]; then
  git -C FlameGraph fetch --depth 1 origin "$FLAMEGRAPH_COMMIT" \
    && git -C FlameGraph checkout --detach "$FLAMEGRAPH_COMMIT"
fi
[ "$(git -C FlameGraph rev-parse HEAD)" = "$FLAMEGRAPH_COMMIT" ] \
  || { echo "ERROR: FlameGraph commit mismatch; refuse to use unreviewed scripts" >&2; exit 1; }
# 生产/隔离不足的目标机不要直接从公网拉取——预置内网镜像或打包分发后校验
# 生成火焰图步骤尽量以非 root 运行（perf.data 可读即可），避免以 root 执行第三方脚本：
perf script -i perf.data > perf.unfold
FlameGraph/stackcollapse-perf.pl perf.unfold > perf.folded
FlameGraph/flamegraph.pl perf.folded > flame.svg

# 3. 热点函数排名
perf report -i perf.data --stdio --no-children | head -40

# 4. 每核 CPU 分布（看 %soft/%irq/%idle）
mpstat -P $CORES 1 30

# 5. 网卡收发/丢包（固定 10 轮，带时间戳与每秒差值；重点 rx_dropped、rx_missed）
prev_ts=$(date +%s)
prev_rx=$(cat /sys/class/net/$ETH/statistics/rx_packets)
prev_tx=$(cat /sys/class/net/$ETH/statistics/tx_packets)
prev_rd=$(cat /sys/class/net/$ETH/statistics/rx_dropped)
prev_rm=$(cat /sys/class/net/$ETH/statistics/rx_missed_errors 2>/dev/null || echo 0)
for i in $(seq 1 10); do
  sleep 1
  now=$(date +%s); rx=$(cat /sys/class/net/$ETH/statistics/rx_packets)
  tx=$(cat /sys/class/net/$ETH/statistics/tx_packets)
  rd=$(cat /sys/class/net/$ETH/statistics/rx_dropped)
  rm=$(cat /sys/class/net/$ETH/statistics/rx_missed_errors 2>/dev/null || echo 0)
  awk -v t="$now" -v dt="$((now-prev_ts))" -v rxd="$((rx-prev_rx))" -v txd="$((tx-prev_tx))" \
      -v rdd="$((rd-prev_rd))" -v rmd="$((rm-prev_rm))" \
      'BEGIN{printf "ts=%s rx_pps=%.0f tx_pps=%.0f rx_dropped_delta=%d rx_missed_delta=%d\n", t, rxd/dt, txd/dt, rdd, rmd}'
  prev_ts=$now; prev_rx=$rx; prev_tx=$tx; prev_rd=$rd; prev_rm=$rm
done

# 6. TCP 层确认：目标连接 RTT + 同一窗口内的重传率（分母=发出段数）
ss -it state established '( sport = :443 )' \
  | awk '{for(i=1;i<=NF;i++) if($i ~ /^rtt:/) print "rtt=" $i}'
r1=$(nstat -az TcpRetransSegs | awk '/TcpRetransSegs/{print $2}')
s1=$(nstat -az TcpOutSegs | awk '/TcpOutSegs/{print $2}')
sleep 10
r2=$(nstat -az TcpRetransSegs | awk '/TcpRetransSegs/{print $2}')
s2=$(nstat -az TcpOutSegs | awk '/TcpOutSegs/{print $2}')
awk -v r1="$r1" -v r2="$r2" -v s1="$s1" -v s2="$s2" \
  'BEGIN{seg=s2-s1; ret=r2-r1; rate=(seg>0)?100*ret/seg:0; printf "retrans_segs=%d out_segs=%d retrans_rate=%.3f%%\n", ret, seg, rate}'
```

> perf report 内核符号显示 `[unknown]` 时：**优先用用户态符号（nginx/libc 函数名）做热点对策匹配**。确需内核符号时，仅在隔离压测机/性能基线机、且用户确认后临时调整：先 `cat /proc/sys/kernel/kptr_restrict` 记录原值，再 `sysctl -w kernel.kptr_restrict=0`；结束后立即 `sysctl -w kernel.kptr_restrict=<原值>` 恢复。生产与共享/多租户节点禁止该变更；perf 权限优先 CAP_PERFMON/`perf_event_paranoid`，不默认整链 root。

## accept 风暴确认（不能只看 master 核满载）

master 核满载可能来自同核其他任务。按以下顺序确认：

```bash
# 1. 先看是谁在占 CPU（排除同核其他进程）
ps -eo pid,comm,psr,%cpu --sort=-%cpu | head
# 2. worker 的 accept 热点（perf report 采样窗口内）
perf report -i perf.data --stdio --no-children | grep -E 'ngx_event_accept|ngx_http_wait_request_handler' | head
# 3. 监听队列溢出/丢弃（同一窗口前后对比）
nstat -az TcpExtListenOverflows TcpExtListenDrops
# 4. 队列积压
ss -lnt | awk 'NR==1 || $2>0'
```

只有 nginx worker 占用 master 核、accept 热点成立且监听队列有溢出/丢包时，才路由 reuseport/核数规划；否则先排查同核其他任务。

## nginx on ARM 典型热点与对策

> **真源声明**：瓶颈类别判定与对策路由以本 skill `SKILL.md §2` 判定树为准（单一事实源）；本表补充火焰图特征与根因细节，对策列仅展开具体命令/配置项，两处不一致时以判定树为准。

| 热点（火焰图特征） | 根因 | 对策 |
|--------------------|------|------|
| `ngx_ssl_handshake` / `RSA_*` / `EVP_*` 占比高 | TLS 握手/记录层 | 加速级（KAL/KAE）、ECDSA 证书、TLS1.3、session 复用 |
| `epoll_wait`/`ngx_epoll_process_events` 上下文切换多 | 连接建立销毁频繁 | reuseport、multi_accept、开 keepalive |
| `memcpy`/`ngx_writev`/sendfile 路径 | 拷贝开销 | sendfile on、sendfile_max_chunk、open_file_cache |
| softirq `%soft` 高、网卡单队列打满 | 中断不均 | IRQ 绑核 + toeplitz 多队列（环境级） |
| `malloc`/`free`（glibc arena 锁） | 并发分配竞争 | tcmalloc LD_PRELOAD（加速级） |
| master 核 100% | accept 风暴 | 核数规划 N-1 + reuseport |

对策执行归属（按 skill 名定位；`nginx-conf-tuning` / `nginx-env-tuning` / `nginx-kunpeng-tuning` 为规划中 !226/!223/!227，合入前分别回退到 `nginx-optimization` 的配置层/L1/L3）：配置级见 `nginx-conf-tuning`，环境级见 `nginx-env-tuning`，加速级见 `nginx-kunpeng-tuning`。

## 参数精调（归属 nginx-conf-tuning）

参数精调的搜索空间与规范（keepalive/sendfile/worker 等单变量网格）统一归属 `nginx-conf-tuning` 的「参数精调」节（规划中 !226；合入前以现有 `nginx-optimization` 配置层 playbook 为准）——本 skill 仅输出精调方向路由。基线与验证纪律见 `nginx-baseline-setup`（!218；未安装时回退 `nginx-optimization` 基线章节）。
