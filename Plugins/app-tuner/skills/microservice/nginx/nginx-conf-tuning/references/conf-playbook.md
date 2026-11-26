# Nginx 配置级调优 Playbook（nginx.conf 场景化模板）

> 本文件是 `nginx-conf-tuning` SKILL.md 的执行细节层：场景差异决策表见 SKILL.md。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

## 通用骨架（全场景生效）

场景差异项见 SKILL.md §2 决策表。

```nginx
user  nginx;                              # 发行版默认非特权账号（openEuler: nginx）；不要用 root 跑 worker
                                          # root 仅限一次性隔离压测机且用户单独确认后才可启用：
                                          #   user root;
                                          # 压测结束恢复后验证：ps -o user,pid,cmd -C nginx   # worker 应回到 nginx
worker_processes  <N>;                   # short=核数N；long=max(1, 核数N-1)（见决策表）
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

## HTTPS 场景 TLS 配置

```nginx
server {
    listen 443 ssl [reuseport];          # 长连接场景加 reuseport
    ssl_certificate     /path/server.crt;
    ssl_certificate_key /path/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;        # 与决策表一致：只留 1.2+1.3；1.3 握手 1-RTT 收益大
    ssl_ciphers "EECDH+ECDSA+AESGCM EECDH+aRSA+AESGCM EECDH+ECDSA+SHA384 EECDH+ECDSA+SHA256 EECDH+aRSA+SHA384 EECDH+aRSA+SHA256 EECDH EDH+aRSA !aNULL !eNULL !LOW !3DES !MD5 !EXP !PSK !SRP !DSS !RC4";
    ssl_prefer_server_ciphers on;
    # ssl_session_cache 按决策表：短连接默认 off；TLS1.2 客户端会带 session ID 重连时改 shared:SSL:1m 并用 $ssl_session_reused 验证
}
```
- 优先 **ECDSA 证书**（ECC 运算量远低于 RSA，鲲鹏上握手吞吐显著更高）
- 证书/私钥自签生成（压测环境）：
  - ECDSA P-256（优先验证路径）：
    `openssl ecparam -name prime256v1 -genkey -noout -out server.key && openssl req -x509 -new -key server.key -out server.crt -days 365 -subj "/CN=localhost"`
  - RSA 2048（对照路径，不是 ECDSA 收益证据）：
    `openssl req -x509 -newkey rsa:2048 -nodes -keyout server-rsa.key -out server-rsa.crt -days 365 -subj "/CN=localhost"`
  - 压测前核对：`openssl x509 -in server.crt -noout -text | grep 'Public Key Algorithm'`（ECDSA 应为 `id-ecPublicKey`）

## Docker/容器场景

- worker 数 = 容器配额核数，`worker_cpu_affinity` 按 cgroup v2 `cat /sys/fs/cgroup/cpuset.cpus.effective`（或当前进程 cgroup 下的同名文件；v1 为 `cpuset.cpus`）实际可用核生成掩码；读不到时用 `grep Cpus_allowed_list /proc/self/status`
- master 用 `numactl -C $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF` 启动
- 宿主机网络模块与网络模式选择见 `nginx-env-tuning`（!223）
- 静态文件场景保持 `open_file_cache` 开启；"no_cache" 仅用于对比诊断

## 配置生效与回滚

```bash
cp -p $CONF $CONF.bak.$(date +%Y%m%dT%H%M%S)   # 每项变更前备份当前生效版本
$NGINX_DIR/sbin/nginx -t -c $CONF -p $NGINX_DIR   # 与目标实例相同的二进制/配置/前缀
$NGINX_DIR/sbin/nginx -s reload -c $CONF -p $NGINX_DIR   # 普通变更只做平滑重载
# 生效核对：worker 数、ps -o psr -C nginx、taskset -pc <worker-pid>、一次实际请求
# 到此结束。只有参数明确要求整实例重启时才进入下面的显式分支（FULL_RESTART=1）：
if [ "${FULL_RESTART:-0}" = "1" ]; then
  MASTER_PID=$(cat "$NGINX_PID_FILE")
  tr '\0' ' ' < "/proc/$MASTER_PID/cmdline" | grep -Fq -- "-c $CONF"   # 核验目标 master
  tr '\0' ' ' < "/proc/$MASTER_PID/cmdline" | grep -Fq -- "-p $NGINX_DIR"
  kill -QUIT "$MASTER_PID"                                            # 按该实例 pidfile 停止，禁止 pkill
  # 按实际场景设置：HTTP 用 LISTEN_PORT=80 HEALTH_URL=http://127.0.0.1:80/
  # HTTPS 自签用 HEALTH_CURL_OPTS=-ksS；生产证书用 --cacert <ca.pem> 而非 -k
  LISTEN_PORT="${LISTEN_PORT:-443}"
  HEALTH_URL="${HEALTH_URL:-https://127.0.0.1:${LISTEN_PORT}/}"
  HEALTH_CURL_OPTS="${HEALTH_CURL_OPTS:--ksS}"
  for _ in $(seq 1 30); do ss -lnt "( sport = :${LISTEN_PORT} )" | grep -q . || break; sleep 1; done
  if ss -lnt "( sport = :${LISTEN_PORT} )" | grep -q .; then
    echo "ERROR: port ${LISTEN_PORT} still held after master exit; refuse to start a second instance" >&2; exit 1
  fi
  taskset -c $MASTER_CORE $NGINX_DIR/sbin/nginx -c $CONF -p $NGINX_DIR
  # shellcheck disable=SC2086
  curl -fsS ${HEALTH_CURL_OPTS} -o /dev/null "$HEALTH_URL" || { echo "ERROR: restart health check failed: $HEALTH_URL" >&2; exit 1; }
fi
# 回退：cp -p $CONF.bak.<ts> $CONF → 同一实例 nginx -t → reload → 一次请求确认
```

基线复测与增益归因纪律见 `nginx-baseline-setup`（!218）；TLS 握手进一步加速（KAL/KAE）见 `nginx-kunpeng-tuning`（规划中 !227；合入前 `nginx-optimization` L3 层）。

## 参数精调（搜索空间与规范）

> 本节是 nginx.conf 参数精调的**单一事实源**（nginx-bottleneck-analysis 的精调方向路由到此处）；决策表（§2）给出各场景基线值，本节给出围绕基线值的单变量搜索空间。

对关键参数做单变量网格搜索（其余保持已验证配置，避免组合爆炸）：

```
keepalive_timeout:   65   → 搜索 50; 100; 300
keepalive_requests:  2000 → 搜索 1000; 10000（短连接）或 100000; 200000（长连接）
tcp_nopush:          on   → on/off 对比
sendfile_max_chunk:  512k → 搜索 256k; 512k; 2048k
worker_processes:    N    → max(1,N-1) / N / N+1（长连接场景重点验证 max(1,N-1)，单核时不得为 0）
```

方法：每参数点 ≥3 轮取均值，只与“除该参数外其余配置相同的当前对照”比较，增益 <2% 即回退；与 L0 的累计增益单列，不参与单项保留判断。多个有增益参数最终合并后整体复测一次。
