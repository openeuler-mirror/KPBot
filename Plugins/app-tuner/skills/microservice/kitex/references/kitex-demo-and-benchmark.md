# Kitex Demo 搭建与专属测量注意事项

通用测量口径（固定负载、预热、指标、判定、回退）以 `microservice-ab-testing` 为准。本文只补 Kitex 侧的具体做法。

## 1. 环境准备（不改系统配置）

```bash
# 1) 工具链：版本与目标服务保持一致，从 https://golang.google.cn/dl/ 获取对应包
GO_VERSION=<version>
mkdir -p ~/kitex-demo ~/toolchain ~/logs && cd ~/toolchain
curl -fL -o "go$GO_VERSION.tar.gz" "https://golang.google.cn/dl/go$GO_VERSION.linux-arm64.tar.gz"
tar -xzf "go$GO_VERSION.tar.gz" && rm "go$GO_VERSION.tar.gz"
export GO_BIN="$PWD/go/bin"

# 2) 加载环境：工具链按 GO_BIN / GOROOT / PATH 解析，不写死目录
. <专题>/assets/env.sh
go version

# 3) 安装工具并记录实际版本，固定到本轮基线
go install github.com/cloudwego/kitex/tool/cmd/kitex@latest
go install github.com/cloudwego/thriftgo@latest
kitex --version; thriftgo --version
```

`env.sh` 的解析顺序：`GO_BIN` → `GOROOT` → PATH 上已有的 `go`；三者都没有时直接报错，避免静默用错版本。也可以 `GOROOT=/path/to/go . <专题>/assets/env.sh`，或确保 `go` 在 PATH 上后直接 source。

**工具链基线**：本专题 demo 的基线使用 Go 1.24.x（示例 1.24.6）；更高 patch（如 1.26.x）只做兼容性穿刺，不改变基线，也不与基线结果混在同一张对照表里。穿刺需记录：编译是否通过、运行告警（如动态编解码库声明的版本范围）、关键路径是否可用。

- 穿刺记录示例：Go 1.26.x 下 Thrift 路径可编译并运行；动态编解码库仍提示仅支持到 1.24，泛化调用路径需单独验证。

国内可达镜像：`golang.google.cn`、`goproxy.cn`；`proxy.golang.org` 可能不可达。Go 与工具链版本以目标服务实际使用为准，不要跨大版本对比。

## 2. 生成与构建

```bash
cd ~/kitex-demo
cp <专题>/assets/echo.thrift .
kitex -module demo -service echo -thrift frugal_tag ./echo.thrift
cp <专题>/assets/handler.go . && cp <专题>/assets/server_main.go main.go
mkdir -p cmd/bench && cp <专题>/assets/bench_main.go cmd/bench/main.go
cp <专题>/assets/run_one.sh . && chmod +x run_one.sh
go mod tidy && go build -o bin/server . && go build -o bin/bench ./cmd/bench
```

`run_one.sh` 也可用 `bash run_one.sh` 调用，避免复制后执行位丢失。它会先检查端口占用、只清理本轮启动的进程、按名称隔离结果文件，并把本轮 bench 的退出码返回给调用方。

**结果字段**：`valid` 为假表示本轮不可用；`aborted` 表示错误率过高提前结束；`samples_capped` 表示延迟样本达到内存预算上限。`aborted` 与 `samples_capped` 的轮次不用于收益对比，只能用于判断"服务是否可用/参数是否合法"。

## 3. Kitex 侧必须固定的开关

| 开关 | 位置 | 为什么必须固定 |
|---|---|---|
| 传输协议 | client `WithTransportProtocol`、server 默认兼容 | 两侧不一致会退化或报错 |
| 编解码 | 两侧 `WithPayloadCodec` | 不一致时回退到 Apache Thrift，数字不可比 |
| 是否复用连接 | 两侧 `WithMuxConnection` / `WithMuxTransport` | 改变连接容量与长尾 |
| 事件循环线程数 | server `netpoll.Configure` | 默认值随并行度变化 |
| 埋点级别 | 两侧 `WithStatsLevel` | 有链路追踪时会自动升到详细级别 |
| 并行度 / GC | 环境变量 | 容器与物理机行为不同 |

## 4. 绑定与角色分离

- 服务端与压测端分开绑核；压测端 CPU 使用率应低于 60%，否则测的是客户端。
- 取偶数核：`cpu0/cpu1` 通常是同一物理核的两个 SMT 线程，同一核上跑 server 与 bench 会互相干扰。
- 同机回环只反映框架与内核成本；跨机需重做基线。

## 5. 观测器与测量的分离

pprof、系统调用统计、硬件计数都会显著降速。正式对比必须在无观测器时跑，观测数据只用于定性定位。

## 6. 常见坑

1. `perf report --stdio` 在部分 openEuler 环境会挂起，用 `perf stat` + pprof 替代。
2. 共享节点上其他负载会占用同一批 CPU，必须多次取中位并记录最小值。
3. 长跑不回收连接会掩盖连接池问题，压测时长要覆盖连接建立与回收。
4. `run_one.sh` 依赖 `/proc/<pid>/stat` 的 CPU 时间，进程被 OOM 或提前退出时数字无效。
5. 并行压测请用不同端口：端口被占用时脚本直接报错，不会杀掉已有进程。
6. 配置名只能是单个文件名（不含 `/`、`..`），否则脚本拒绝执行，避免结果写到 `results/` 之外。
7. 远端执行走 `remote-runner`，不要把 SSH 命令写进各 skill。
