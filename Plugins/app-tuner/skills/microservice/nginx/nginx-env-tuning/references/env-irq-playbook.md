# Nginx 环境级调优 Playbook（系统与网卡命令模板）

> 本文件是 `nginx-env-tuning` SKILL.md 的执行细节层：布局决策表与变更清单见 SKILL.md。
> 经验数据来源：鲲鹏平台 nginx 压测实测（kpbot-dev-journal `a+k/bale/nginx/`）。

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

# 2. 文件句柄（三层配套：启动会话 ulimit / systemd LimitNOFILE / nginx worker_rlimit_nofile）
ulimit -n 102400       # 只影响本会话之后启动的进程；对已运行 nginx 无效
# systemd 单元：LimitNOFILE=102400（daemon-reload 后重启服务）
# nginx.conf：worker_rlimit_nofile 102400;（worker 启动时读取，需重启/平滑重载生成新 worker）
# 生效核对：cat /proc/<worker-pid>/limits | grep 'open files'

# 3. （可选）cgroup 限内存，避免内存抖动
cgcreate -g memory:/nginxgrp
cgclassify -g memory:/nginxgrp $(pidof nginx)
cgset -r memory.limit_in_bytes=8G /nginxgrp
cgset -r memory.memsw.limit_in_bytes=8G /nginxgrp
```

## 网卡多队列与中断绑核（关键）

worker/IRQ 布局按 SKILL.md §2 场景表选择，命令如下：

```bash
# 查看网卡队列数，按需开启 RSS 多队列（以 Intel/Mellanox 为例）
ethtool -l $ETH                 # 查看当前/最大队列
ethtool -L $ETH combined $IRQ_NUM   # 设置队列数

# RSS 哈希：toeplitz + 多队列分发
ethtool -X $ETH hfunc toeplitz equal $IRQ_NUM
# 固定随机 key 也可用：ethtool -X $ETH hkey <40字节hex> hfunc toeplitz equal $IRQ_NUM

# 手工绑定每个网卡队列中断到指定核——按队列索引逐队列分配不同掩码（与 SKILL.md §2
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

## Docker/容器场景补充

- 宿主机预加载网络模块（缺省容器场景常见损耗）：`modprobe nf_conntrack iptable_nat iptable_filter`
- 容器网络性能：host 网络 > macvlan > bridge；bridge 模式检查 conntrack 表是否打满（`dmesg | grep conntrack`）
- worker 绑核掩码按容器实际可用核生成（cgroup v2 `cpuset.cpus.effective.cpus` / v1 `cpuset.cpus`）

基线复测与增益归因纪律见 `nginx-baseline-setup`（!218；未安装时回退 `nginx-optimization` 的基线章节）。
