# Nginx 调优 Playbook（L1/L2/L3 命令与配置模板）

> 本文件是 `nginx-optimization` SKILL.md 的执行细节层：分层决策表与验证纪律见 SKILL.md，此处提供各层完整命令与配置模板。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

## L1 环境层优化（系统与网卡）

通用流程。注意：**服务停用与 IRQ 写入是系统级变更**——仅一次性压测机/性能基线机执行，
生产与共享/多租户节点须先向用户确认；执行前记录状态存档，结束后按恢复命令还原。

```bash
# 1. 关闭干扰服务（先存档当前状态，压测结束按存档恢复）
SNAP=${NGINX_DIR:-/tmp}/rollback_snapshot; mkdir -p $SNAP
systemctl is-active irqbalance firewalld | tee $SNAP/svc_state.txt   # 变更前状态存档
systemctl stop irqbalance     # 避免中断漂移，改为手工绑核（恢复：systemctl start irqbalance）
systemctl stop firewalld      # 消除防火墙 netfilter 开销（恢复：systemctl start firewalld）
# 压测结束恢复并验证：
#   systemctl start irqbalance firewalld
#   systemctl is-active irqbalance firewalld   # 应与 $SNAP/svc_state.txt 一致

# 2. 文件句柄（必须在启动 nginx 的同一会话设置，或 systemd 单元加 LimitNOFILE=102400）
ulimit -n 102400

# 3. （可选）cgroup 限内存，避免内存抖动
cgcreate -g memory:/nginxgrp
cgclassify -g memory:/nginxgrp $(pidof nginx)
cgset -r memory.limit_in_bytes=8G /nginxgrp
cgset -r memory.memsw.limit_in_bytes=8G /nginxgrp
```

### L1.1 网卡多队列与中断绑核（关键）

worker/IRQ 布局按 SKILL.md §3.1 场景表选择，命令如下：

```bash
# 查看网卡队列数，按需开启 RSS 多队列（以 Intel/Mellanox 为例）
ethtool -l $ETH                 # 查看当前/最大队列
ethtool -L $ETH combined $IRQ_NUM   # 设置队列数

# RSS 哈希：toeplitz + 多队列分发
ethtool -X $ETH hfunc toeplitz equal $IRQ_NUM
# 固定随机 key 也可用：ethtool -X $ETH hkey <40字节hex> hfunc toeplitz equal $IRQ_NUM

# 手工绑定每个网卡队列中断到指定核——按队列索引逐队列分配不同掩码（与 SKILL.md §3.1
# 每队列分核布局一致；不要给所有队列写同一个掩码，那会让中断聚到单核）
# 部分驱动的 /proc/interrupts 行名用驱动标签而非接口名；按接口名 grep 无结果时先取驱动名再匹配：ethtool -i $ETH | awk '/driver/{print $2}'
CORES_LIST="0 1 2 3"    # 按场景布局表给核列表（如 http_short(4T)=核 2-3 → "2 3"；核数=队列数）
NQ=$(grep -c "$ETH" /proc/interrupts)
i=0
for irq in $(grep $ETH /proc/interrupts | awk -F: '{print $1}'); do
    core=$(echo $CORES_LIST | awk -v k=$((i % $(echo $CORES_LIST | wc -w))) '{print $(k+1)}')
    mask=$(printf '%x' $((1 << core)))             # 核 core 的 16 进制单核位掩码
    echo $mask > /proc/irq/$irq/smp_affinity
    echo "queue$i -> core$core (mask=$mask)"
    i=$((i+1))
done
# 队列数多于核列表时按上式取模轮回复用；需要严格一一对应则先用 ethtool -L 收缩队列数
# 回退：echo ff > /proc/irq/$irq/smp_affinity（恢复全核可调度）
```

绑核后校验判据（每队列增量必须落在绑定的核上）：

```bash
grep "$ETH" /proc/interrupts > /tmp/irq.before
# …压测 30s…
grep "$ETH" /proc/interrupts > /tmp/irq.after
# 打印每个队列的增量最大 CPU 列（应等于绑定核，且其余列增量 ≈ 0）
awk 'NR==FNR{for(i=2;i<=NF;i++) b[FNR","i]=$i; next}
     {mx=0; mc=-1; for(i=2;i<=NF;i++){d=$i-b[FNR","i]; if(d>mx){mx=d; mc=i-2}}
      printf "%s 增量最大: CPU%d (+%d)\n", $1, mc, mx}' /tmp/irq.before /tmp/irq.after
```

## L2 配置层优化（nginx.conf 场景化模板）

### L2.1 通用骨架（全场景生效）

场景差异项见 SKILL.md §3.2 决策表。

```nginx
user  nginx;                              # 发行版默认非特权账号（openEuler: nginx）；不要用 root 跑 worker
                                          # root 仅限一次性隔离压测机且用户单独确认后才可启用：
                                          #   user root;
                                          # 压测结束恢复后验证：ps -o user,pid,cmd -C nginx   # worker 应回到 nginx
worker_processes  <N>;                   # short=核数N；long=核数N-1（见决策表）
worker_cpu_affinity <掩码1> <掩码2> ...;  # 每 worker 绑 1 核。掩码是二进制字符串（非十六进制）：
                                          #   核0=1 核1=10 核2=100 核3=1000；核8=100000000（nginx 只接受 0/1 字符）
worker_rlimit_nofile 102400;
error_log  /dev/null;                    # 压测消除日志开销；生产降级为 warn

events {
    use epoll;                 # Linux 事件模型
    worker_connections 102400;
    accept_mutex off;          # 关闭多 worker 抢 accept 锁
    multi_accept on;           # 一次惊醒尽量多收连接（短连接收益大）
}

http {
    access_log  off;           # 压测必须关；生产降采样
    sendfile        on;        # 静态文件零拷贝（http 场景收益大）
    tcp_nopush      on;        # 配合 sendfile 聚包
    tcp_nodelay     on;        # 禁用 Nagle，降低小包时延
    server_tokens   off;
    sendfile_max_chunk 512k;   # 避免单连接长时间霸占 worker
    client_header_buffer_size 4k;
    large_client_header_buffers 4 32k;
    server_names_hash_bucket_size 128;
    open_file_cache max=102400 inactive=40s;   # 文件句柄/元数据缓存
    open_file_cache_valid 50s;
    open_file_cache_min_uses 1;
    open_file_cache_errors on;

    server {
        listen 80 reuseport;   # reuseport：每 worker 独立 listen 队列，核数扩展性关键
        ...
    }
}
```

### L2.2 HTTPS 场景 TLS 配置

```nginx
server {
    listen 443 ssl [reuseport];          # 长连接场景加 reuseport
    ssl_certificate     /path/server.crt;
    ssl_certificate_key /path/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;        # 与决策表一致：只留 1.2+1.3；1.3 握手 1-RTT 收益大
    ssl_ciphers "EECDH+ECDSA+AESGCM EECDH+aRSA+AESGCM EECDH+ECDSA+SHA384 EECDH+ECDSA+SHA256 EECDH+aRSA+SHA384 EECDH+aRSA+SHA256 EECDH EDH+aRSA !aNULL !eNULL !LOW !3DES !MD5 !EXP !PSK !SRP !DSS !RC4";
    ssl_prefer_server_ciphers on;
    # ssl_session_cache 按决策表：短连接 off，长连接 shared:SSL:1m
}
```
- 优先 **ECDSA 证书**（ECC 运算量远低于 RSA，鲲鹏上握手吞吐显著更高）
- 证书/私钥自签生成（压测环境）：
  `openssl req -x509 -newkey rsa:2048 -nodes -keyout server.key -out server.crt -days 365 -subj "/CN=localhost"`

### L2.3 Docker/容器场景

- worker 数 = 容器配额核数，`worker_cpu_affinity` 按 `cat /sys/fs/cgroup/cpuset/cpus.effective.cpus`（cgroup v2）或 `cpuset.cpus`（v1）实际可用核生成掩码
- master 用 `numactl -C $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF` 启动
- 宿主机预加载网络模块（缺省容器场景常见损耗）：`modprobe nf_conntrack iptable_nat iptable_filter`
- 容器网络性能：host 网络 > macvlan > bridge；bridge 模式检查 conntrack 表是否打满（`dmesg | grep conntrack`）
- 静态文件场景保持 `open_file_cache` 开启；"no_cache" 仅用于对比诊断

### L2.4 配置生效与回滚

```bash
$NGINX_DIR/sbin/nginx -t -c $CONF        # 语法校验，必做
$NGINX_DIR/sbin/nginx -s reload          # 平滑重载（部分参数如 worker_cpu_affinity 需重启）
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF   # 干净重启（pidfile 作用域停止；pkill 会误杀同机所有 nginx 实例，多实例节点禁用）
```

## L3 鲲鹏加速层优化（库 / 硬件加速）

L3 属**运行时/库替换类高危变更**：每步必须按 Safety Gates 执行——先快照（L3.0）、再变更、
验证生效、A/B 证实收益后才保留；任一环节失败立即按回退步骤恢复，禁止跳步叠加。
多租户/共享节点上驱动与环境变量类变更影响全机，执行前必须确认占用情况。

### L3.0 前置快照（所有 L3 步骤共用，必做）

```bash
SNAP=$NGINX_DIR/rollback_snapshot; mkdir -p $SNAP
cp -a $NGINX_DIR/sbin/nginx $SNAP/nginx.orig            # nginx 二进制快照
sha256sum $NGINX_DIR/sbin/nginx | tee $SNAP/nginx.sha256
$NGINX_DIR/sbin/nginx -V 2>&1 | tee $SNAP/nginx-V.txt    # 编译参数存档（回退对比与重建依据）
cp -a $CONF $SNAP/nginx.conf.orig                       # 配置快照
env | sort > $SNAP/launch.env                           # 启动环境基线（LD_PRELOAD 等应此刻为空）
systemctl cat nginx 2>/dev/null > $SNAP/nginx.unit      # systemd 单元存档（若有）
```

### L3.1 KAL OpenSSL（鲲鹏加速版 OpenSSL，https 场景核心收益）

**风险**：替换 OpenSSL 供给——`LD_LIBRARY_PATH` 对 nginx 及其子进程全部生效，与发行版
OpenSSL 混链可能符号冲突/ABI 不一致；重编译的 nginx 与原二进制行为差异需整轮回归；
编译期 `--with-openssl` 变更**只能靠恢复原二进制回退**，环境变量无法撤销。

```bash
# 编译期：nginx 源码目录，链接 KAL OpenSSL
./configure --with-http_ssl_module --prefix=$NGINX_DIR --with-openssl=<KAL_OPENSSL_SRC>
# 若 Makefile 引用 .openssl 静态库路径，改写为：
#   -L<KAL_OPENSSL_PATH>/lib -lcrypto -lssl
make && make install

# 运行期：
export LD_LIBRARY_PATH=<KAL_OPENSSL_PATH>/lib:$LD_LIBRARY_PATH
export PATH=<KAL_OPENSSL_PATH>/bin:$PATH
taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
```

**生效验证**：
```bash
ldd $NGINX_DIR/sbin/nginx | grep libssl    # 应指向 <KAL_OPENSSL_PATH>/lib 的 kal 版本
sha256sum $NGINX_DIR/sbin/nginx            # 与 $SNAP/nginx.sha256 对比，确认确为新二进制
$NGINX_DIR/sbin/nginx -t -c $CONF          # 依赖加载门控
```
收益以 https 压测 A/B 为准（SKILL.md §4 纪律）。

**回退**：
```bash
cp -a $SNAP/nginx.orig $NGINX_DIR/sbin/nginx
sha256sum $NGINX_DIR/sbin/nginx | diff - <(cut -d' ' -f1 $SNAP/nginx.sha256) && echo 二进制恢复一致
unset LD_LIBRARY_PATH                      # 或按 $SNAP/launch.env 恢复原环境
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
$NGINX_DIR/sbin/nginx -V 2>&1 | diff - $SNAP/nginx-V.txt && echo 编译参数回到基线
```

### L3.2 KAE / UADK 硬件加速引擎（hisi_hpre 等，需鲲鹏加速器硬件）

**风险**：`modprobe -r hisi_hpre` 卸载驱动影响**全机所有占用该加速器的进程**（多租户节点
高风险）；`uacce_mode=2` 改变设备直通模式；`OPENSSL_CONF`/`OPENSSL_ENGINES` 影响本会话
所有 openssl 调用方。

```bash
# 卸载前确认无占用：任一命令有输出即有进程/绑定占用，禁止继续
fuser -v /dev/uacce* 2>&1
ls /sys/class/uacce/                       # 非空 = 存在用户态绑定
# 确认无占用后再切换模式：
modprobe -r hisi_hpre && modprobe hisi_hpre uacce_mode=2    # 驱动切用户态直通模式
export OPENSSL_ENGINES=/usr/lib64/engines-1.1               # 或 KAL 的 engines 目录
export OPENSSL_CONF=<openssl_kae.cnf>                       # 引用 kaectl 生成的引擎配置
```

**生效验证**：`openssl engine -t` 显示 kae 引擎可用；nginx `error_log` 无 engine 加载错误；
https 压测 A/B 确认收益。无硬件时整节跳过。

**回退**：
```bash
modprobe hisi_hpre                         # 恢复默认模式（不带 uacce_mode=2）
ls /sys/class/uacce/                       # 确认设备节点回归
unset OPENSSL_ENGINES OPENSSL_CONF
```

### L3.3 tcmalloc（替换 glibc malloc；nginx 自带内存池，收益场景相关，A/B 为准）

**风险**：`LD_PRELOAD` 替换全局分配器，影响 nginx master 与全部 worker 及其子进程；与其他
预加载分配器（jemalloc 等）互斥；库版本与运行时不匹配可致段错误（nginx 崩溃 = 服务中断）。

```bash
# 优先使用发行版包（openEuler：dnf install -y gperftools-libs，提供 /usr/lib64/libtcmalloc.so.4）
# 发行版确无包时源码编译——固定发布 tag，禁止 master HEAD：
git clone https://github.com/gperftools/gperftools && cd gperftools
git checkout gperftools-2.9.1 && git rev-parse HEAD | tee gperftools.commit   # 记录固定版本
./autogen.sh && ./configure --prefix=/opt/gperftools-2.9.1 && make -j && make install
# 前缀安装（不写入系统目录），注入时使用绝对路径，避免与发行版库混淆：
#   export LD_PRELOAD=/opt/gperftools-2.9.1/lib/libtcmalloc.so

# 应用（在启动环境注入后重启）：
export LD_PRELOAD=/usr/lib64/libtcmalloc.so    # 路径按实际安装位置
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
```

**生效验证**：
```bash
grep -c tcmalloc /proc/<nginx-master-pid>/maps   # ≥1 才生效
grep -c tcmalloc /proc/<worker-pid>/maps          # worker 继承 LD_PRELOAD，同样应有映射
```

**回退**：
```bash
unset LD_PRELOAD
nginx -s stop -c $CONF; taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF
grep -c tcmalloc /proc/<nginx-master-pid>/maps || echo "回退确认：无 tcmalloc 映射"
```

### L3.4 xcall 微架构开关（920G 专属，最高风险项）

**风险**：`/proc/self/xcall` 为非公开文档化接口，写入改变微架构行为且影响同核其他进程；
错误值可造成不可预期的全机性能回退；无可读回执、无法查询当前值。**共享节点禁止执行**，
仅 920G 独占环境在确认基线（先做完整基线压测存档）后使用。

```bash
# xcall 内核开关序列（920G 微架构调优，逐个写入）：
for sc in 63 64 242 172 21 67 207 208 22 66 57 71; do
    echo $sc > /proc/self/xcall 2>/dev/null || true
done
```

**生效验证**：无回执接口，仅以压测 A/B 前后对比作为生效与收益证据。
**回退**：写入值重启即失效——重启主机恢复默认；不确定历史写入影响时同样以重启消除。

## 热点分析与深度优化

当分层调优仍不达标时，用 perf 火焰图 + mpstat + 网卡统计定位（以下命令自包含）：

```bash
# 1. perf 采样（30s @99Hz，覆盖 worker 核）
perf record -F 99 -C $CORES -a -g -o perf.data -- sleep 30

# 2. 生成火焰图（FlameGraph 工具一次性获取）
# FlameGraph：固定 commit 便于复现与审阅（纯 perl 脚本、无构建无安装，可先读再跑）；
# 生产/隔离不足的目标机不要直接从公网拉取——预置内网镜像或打包分发后校验
[ -d FlameGraph ] || { git clone https://github.com/brendangregg/FlameGraph.git && \
  (cd FlameGraph && git rev-parse HEAD | tee ../flamegraph.commit); }
# 生成火焰图步骤尽量以非 root 运行（perf.data 可读即可），避免以 root 执行第三方脚本：
perf script -i perf.data > perf.unfold
FlameGraph/stackcollapse-perf.pl perf.unfold > perf.folded
FlameGraph/flamegraph.pl perf.folded > flame.svg

# 3. 热点函数排名
perf report -i perf.data --stdio --no-children | head -40

# 4. 每核 CPU 分布（看 %soft/%irq/%idle）
mpstat -P $CORES 1 30

# 5. 网卡每秒收发/丢包（重点 rx_dropped、rx_missed）
while sleep 1; do cat /sys/class/net/$ETH/statistics/{rx_packets,rx_dropped,tx_packets}; echo; done

# 6. TCP 层确认：重传与 RTT
ss -it | awk '{print $1, $2}' | head; nstat -az TcpRetransSegs
```

> perf report 内核符号显示 `[unknown]` 时：确认 `kernel.kptr_restrict=0` 且以 root 运行（部分环境还需 CAP_SYSLOG/完整 vmlinux）；无法解析时以用户态符号（nginx/libc 函数名）做热点对策匹配即可。

**nginx on ARM 典型热点与对策**：

| 热点（火焰图特征） | 根因 | 对策 |
|--------------------|------|------|
| `ngx_ssl_handshake` / `RSA_*` / `EVP_*` 占比高 | TLS 握手/记录层 | L3 加速、ECDSA 证书、TLS1.3、session 复用 |
| `epoll_wait`/`ngx_epoll_process_events` 上下文切换多 | 连接建立销毁频繁 | reuseport、multi_accept、开 keepalive |
| `memcpy`/`ngx_writev`/sendfile 路径 | 拷贝开销 | sendfile on、sendfile_max_chunk、open_file_cache |
| softirq `%soft` 高、网卡单队列打满 | 中断不均 | IRQ 绑核 + toeplitz 多队列（L1.1） |
| `malloc`/`free`（glibc arena 锁） | 并发分配竞争 | tcmalloc LD_PRELOAD（L3.3） |
| master 核 100% | accept 风暴 | 核数规划 N-1 + reuseport |

## 参数精调（搜索空间与规范）

对关键参数做单变量网格搜索（其余保持已验证配置，避免组合爆炸）：

```
keepalive_timeout:   65   → 搜索 50; 100; 300
keepalive_requests:  2000 → 搜索 1000; 10000（短连接）或 100000; 200000（长连接）
tcp_nopush:          on   → on/off 对比
sendfile_max_chunk:  512k → 搜索 256k; 512k; 2048k
worker_processes:    N    → N-1 / N / N+1（长连接场景重点验证 N-1）
```
方法：每参数点 ≥3 轮取均值，对比基线无增益（<2%）即回退；多个有增益参数最终合并后整体复测一次。
