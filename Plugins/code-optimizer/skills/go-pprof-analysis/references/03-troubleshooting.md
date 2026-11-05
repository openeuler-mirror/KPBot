# 常见排坑清单

按出现频率排序，实测踩过的坑。

## 1. Web UI 报 "Could not execute dot; may need to install graphviz"

调用图（Graph 视图）依赖 graphviz 渲染。

```bash
yum install -y graphviz        # openEuler/CentOS
apt-get install -y graphviz    # Ubuntu/Debian
```

**安装后必须重启 `go tool pprof -http` 进程**（dot 在启动时探测）。火焰图不依赖 graphviz，未安装时可直接用 `/ui/flamegraph`。

## 2. 端口被占用

`go tool pprof -http=:8080` 时 8080 常被 nginx 等占用，且报错不明显。

```bash
ss -tlnp | grep 8080                              # 排查占用
go tool pprof -http=0.0.0.0:8081 cpu.pprof        # 换端口
```

## 3. 远程服务器的 localhost 本地浏览器打不开

`-http=:8080` 只绑定回环地址，指**服务器自己**。两种解法：

```bash
# 方法 1：绑定所有网卡（注意防火墙/安全组，仅限内网）
go tool pprof -http=0.0.0.0:8081 cpu.pprof

# 方法 2：本地终端 SSH 转发（更安全）
ssh -L 8081:localhost:8081 user@<server>
# 保持连接，本地浏览器访问 http://localhost:8081
```

防火墙检查：`iptables -L INPUT -n`（policy ACCEPT 即放行）、`firewall-cmd --list-ports`；云主机还要查安全组。

## 4. CPU profile 采样为空 / Total samples = 0

CPU 采样只记录**正在执行**的栈。抓取 `profile?seconds=N` 期间程序空闲（或压测已结束）→ 采不到数据。
对策：先起负载再抓；抓取期间保持流量；文件式 `StartCPUProfile` 必须包住真正的业务逻辑。

## 5. 后台启动 pprof 服务/进程后连接被拒

`cmd &` 在某些 shell 会话退出时子进程被回收。用 `setsid nohup ... &` 完全脱离会话：

```bash
setsid nohup go tool pprof -http=0.0.0.0:8081 cpu.pprof >/tmp/pprof-web.log 2>&1 < /dev/null &
```

杀进程时注意：`pkill -f "pprof -http"` 可能匹配到自身命令行导致 shell 挂起，用 `pkill -x pprof-demo`（精确进程名）更稳。

## 6. block / mutex profile 永远为空

目标程序必须显式开启：

```go
import "runtime"
runtime.SetBlockProfileRate(1)
runtime.SetMutexProfileFraction(1)
```

## 7. heap 数值与 RSS 对不上

- heap profile 是采样估计（默认 512KB 采一次），且只统计 Go 堆在用量（inuse），不含：栈、cgo 分配、已释放未归还 OS 的部分。
- 看进程真实占用用 `ps -o rss`；看 Go 运行时明细用 `/debug/pprof/` 之外暴露的 `runtime.ReadMemStats` 指标。

## 8. pprof HTTP 端点安全风险

`/debug/pprof/` 会暴露源码路径、命令行参数、甚至可被用于 DoS（CPU profile 期间有开销）。生产环境：
- 绑定内网地址 + 独立端口，或用独立 mux 加鉴权中间件；
- 不要直接挂在对外服务的 mux 上。
