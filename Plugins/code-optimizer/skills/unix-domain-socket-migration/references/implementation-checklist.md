# UDS 迁移实施清单

配合 `SKILL.md` 使用。所有命令按目标系统实际工具调整，执行前先确认权限与影响面。

## 1. 框架支持探查

对链路两端分别回答三个问题，答案要来自目标版本的文档或源码，不能靠猜：

```text
1. 监听：server 是否接受 unix:/path、unix:///path 或独立 unixsocket 配置？
2. 连接：client/channel/upstream 是否接受同样的 UDS 地址？
3. 清理：server 停止时是否自动 unlink socket 文件？版本是否覆盖目标版本？
```

常见形态：地址字符串里的 `unix:` 前缀（如 brpc 的 EndPoint）、独立的 `unixsocket`/`socket` 配置项、框架提供 socket 工厂或自定义 transport 接口。只有一端支持时，先评估另一端的改造量与发布可行性。

以 brpc 为例（仅作形态参考，不替代目标版本核对）：

```text
Endpoint 支持 "unix:/abs/path" 形式，server 与 channel 共用同一套解析；
清理点在启动侧：tcp_listen 在 bind 前按 -reuse_uds_path 标志 unlink 旧路径（默认关闭，且不做 live-listener 检查，与本 skill「启动前先确认无监听者再删」的安全要求相悖，开启需自行评估）；
Server::Stop 各版本都不删除监听路径，停止/崩溃后的清理需要应用层兜底。
```

## 2. 配置模板与一致性检查

```text
# server 配置
bind  unix:/run/myservice/backend.sock

# client 配置
backend-addrs  unix:/run/myservice/backend.sock
```

```bash
# 分别按两端框架的语法解析地址；字符串不必相同
grep -n 'unix:' /etc/myservice/server.conf
grep -n 'unix:' /etc/myservice/client.conf
```

注意：

- 同一共享卷可以在服务端挂到 `/run/backend`、在客户端挂到 `/mnt/backend`，两端绝对路径不同但指向同一个 socket；不要用字符串相等做一致性校验，照抄服务端路径可能让客户端连不上。以客户端实际 `connect` 成功为准。
- 路径含空格或特殊字符在部分框架中需要转义，优先使用短、纯字母数字的路径。
- 端口配置在切 UDS 后可能仍被其他逻辑读取（健康检查、日志、指标），逐一排查。

## 3. 安全清理与生命周期

启动和停止清理共用同一段校验，只删除预期目录中、属主匹配、`lstat` 确认为 socket 的残留项。查询不到监听状态时保持文件并报错，不要继续删除。

```bash
SOCK=/run/myservice/backend.sock
SOCK_DIR=/run/myservice
SERVICE_USER=myservice
# 服务所在 netns 的进程 PID；清理脚本本身就在该 netns 时显式设 SAME_NETNS=1
NETNS_PID=<服务所在 netns 的进程 PID>

cleanup_sock() {
  [ -e "$SOCK" ] || return 0
  [ "$(dirname -- "$SOCK")" = "$SOCK_DIR" ] || { echo "refuse: unexpected dir $SOCK" >&2; return 1; }
  [ "$(realpath -m -- "$SOCK")" = "$SOCK" ] || { echo "refuse: unusual path $SOCK" >&2; return 1; }
  [ -S "$SOCK" ] || { echo "refuse: not a socket: $SOCK" >&2; return 1; }
  [ "$(stat -c %U -- "$SOCK")" = "$SERVICE_USER" ] || { echo "refuse: owner mismatch: $SOCK" >&2; return 1; }

  local listeners
  if [ "${SAME_NETNS:-0}" = "1" ]; then
    listeners=$(ss -xl 2>/dev/null) || {
      echo "cannot query listener state; keeping $SOCK" >&2; return 1; }
  elif [ -n "${NETNS_PID:-}" ]; then
    listeners=$(nsenter -t "$NETNS_PID" -n -- ss -xl 2>/dev/null) || {
      echo "cannot query listener state in service netns; keeping $SOCK" >&2; return 1; }
  else
    echo "cannot confirm service netns; keeping $SOCK" >&2; return 1
  fi
  if awk -v s="$SOCK" '{for (i=1;i<=NF;i++) if ($i==s) {found=1; exit}} END{exit !found}' <<<"$listeners"; then
    echo "listener alive; refuse to remove $SOCK" >&2; return 1
  fi
  rm -f -- "$SOCK"
}

# 启动前、停止后都调用同一函数；服务已退出或尚未启动时才允许清理
cleanup_sock
```

- `ss` 缺失、权限不足、`nsenter` 失败、读不到 PID 或无法确认服务 netns 时，函数返回失败并保留文件；不要用「grep 没匹配」推断没有监听。
- 不要用 `grep -F` 子串判断：`backend.sock.bak` 会命中 `backend.sock`。按 `ss` 输出的空白分隔字段做精确路径匹配。
- 服务运行在自己的 network namespace 时，必须在服务 netns 内查询监听状态；清理脚本在服务 netns 内运行时显式设 `SAME_NETNS=1`，否则提供 `NETNS_PID`。两者都不可用时保留文件，人工确认后再处理。
- pathname socket 的文件系统路径在本端可见即可，监听状态看服务端 netns。
- 只处理 pathname socket；abstract namespace 没有文件需要清理，不需要这一步。
- 运行中永远不要删除 socket 文件。

### 就绪判定

```bash
# 文件存在不等于可连接：从客户端执行带超时的 connect
python3 - "$SOCK" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(1.0)
try:
    s.connect(sys.argv[1])
except OSError as e:
    print(f"not ready: {e}")
    sys.exit(1)
finally:
    s.close()
print("ready")
PY
```

`ss -xl` 只用于人工查看监听者，不能替代 `connect`：服务被 SIGKILL 后 socket 文件仍可能残留，只看文件会让已退出的服务被判健康。需要判断业务就绪时，在 `connect` 之后再发一次最小协议请求。客户端使用自己解析出的 socket 路径（可能与服务端配置字符串不同）执行 `connect`。

## 4. 权限与对端身份

```bash
install -d -m 0750 -o myservice -g myservice /run/myservice
# socket 文件的权限由进程 umask 决定；需要更严时启动后显式设置
chmod 0660 /run/myservice/backend.sock
chgrp myservice /run/myservice/backend.sock
```

- pathname socket 的访问控制来自文件系统：连接方需要对 socket 文件有写权限、对路径目录有进入权限。
- 需要校验对端身份时，在 Linux 上用 `SO_PEERCRED` 取 uid/gid/pid，不要依赖路径或端口。

```c
struct ucred cred;
socklen_t len = sizeof(cred);
if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) == 0) {
    /* cred.uid / cred.gid / cred.pid */
}
```

- abstract namespace socket（地址首字节为 `\0`，部分框架写作 `@name`）不受文件系统权限约束，作用域是 network namespace；是否支持取决于框架，使用前单独确认。

## 5. 容器与部署检查

```text
[ ] 两端能解析到同一个 pathname socket（可用共享卷；两端挂载路径可以不同）
[ ] rootfs 不是只读，或 socket 路径挂载为可写
[ ] 跨 pod 场景确认共享卷可达；跨 host 直接判不适用
[ ] pathname socket 检查共享文件系统可见性；abstract socket 检查同 network namespace、框架支持与对端身份策略
[ ] SELinux/AppArmor 是否允许 AF_UNIX 与目标目录访问
[ ] 多副本路径按实例/租户隔离，避免同机路径冲突
[ ] 探活、服务发现、防火墙、LB 规则不依赖已消失的 TCP 端口
```

## 6. 代码改造骨架（框架不支持时）

```c
/* endpoint 解析：只负责把配置字符串映射到 sockaddr_un */
int parse_unix_endpoint(const char *value, struct sockaddr_un *addr, socklen_t *len);

/* 监听 */
int listen_unix(const char *path) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    /* 长度检查：路径必须小于 sizeof(addr.sun_path) */
    /* bind：失败时区分 EADDRINUSE / EACCES / ENOENT，给出可操作错误 */
    /* listen */
    /* 正常退出时 unlink；异常退出由启动前清理兜底 */
}

/* 连接 */
int connect_unix(const char *path) {
    /* socket + connect；处理 ENOENT / EACCES / ECONNREFUSED */
    /* 支持重试与超时，避免 server 未就绪时直接失败 */
}
```

改造约束：

- TCP 与 UDS 共用同一套读写、超时、重连和协议逻辑，不要复制一套数据面。
- 路径长度、终止符、相对路径与权限错误要有明确日志，避免排障时只能看到「连不上」。
- 补三类测试：等价性（同一组请求与响应）、错误路径（路径不存在/权限不足/残留文件）、重启与并发。

## 7. 发布与回滚

```text
[ ] 先做配置可切换：保留 TCP endpoint，UDS 作为另一份配置
[ ] 灰度单实例/单分片：观察错误率、连接数、延迟、资源
[ ] 两端一起切换：server 与 client 的 endpoint 必须同步发布
[ ] 回退步骤：两端改回 TCP → 重启 → 清理 socket 文件 → 确认服务恢复
[ ] 混合版本窗口：确认旧版本 client 不会连到 UDS-only 的 server
```

## 8. 性能采样命令

```bash
# 内核路径占比（需要 perf 权限）
perf record -g -p <pid> -- sleep 10
perf report --stdio | grep -E 'tcp_|ip_|skb|sock_|unix_'

# 每请求系统调用（限定 10 秒窗口；用同一窗口完成的请求数换算每请求调用次数）
timeout -s INT 10s strace -c -f -p "$pid"

# 连接归属（在目标进程所在 netns 执行；TCP/UDS 二选一）
nsenter -t "$pid" -n -- ss -ntp state established   # TCP：核对两端地址与进程
nsenter -t "$pid" -n -- ss -xp state established    # UDS：核对路径与进程
# ss -x / ss -ltn 只列出本机 socket，不能证明目标连接走的是 loopback
```

`perf` 的 `-- cmd` 可以作为窗口；`strace` 的 `-p` 不能照抄这种写法，必须用 `timeout` 限制窗口，`-s INT` 让 strace 在中断时打印汇总。

采集前记录：负载模型、连接数、消息大小、CPU 绑核、内核版本；采集后把原始命令与结果路径写进验证记录。

## 9. 验证记录模板

```text
链路:                <client → server / 协议>
迁移前 endpoint:     <tcp endpoint>
迁移后 endpoint:     <unix endpoint>
环境:                <机器/内核/框架版本/绑核>
功能验证:            <请求成功/错误路径/重启/并发/清理>
性能验证档位:        read_only | single_run | ab
结果:                <吞吐/p50/p99/CPU usr,sys/错误数>
噪声与结论:          <波动带/是否采纳/回退决定>
未验证项:            <监控接入/多实例/跨容器等>
```
