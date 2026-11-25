---
name: kitex-profiling
description: 当 Kitex 服务只有现象没有方向、需要判断瓶颈类别时使用。交付证据采集清单、特征帧解读与证据充分性判定。
---

# Kitex 证据采集与解读

本 skill 只负责"采什么、怎么读、够不够下结论"。通用 Go 分析方法见 `code-optimizer` 侧 `microservice/go-pprof-analysis`；通用测量口径见 `microservice-ab-testing`。

## 何时触发

- 只有现象没有方向，需要先定位瓶颈类别。
- profile 已有但看不懂 Kitex 相关帧（传输层、派发池、编解码）。
- 需要判断统计/链路追踪埋点是否带来可观开销。
- 需要判断现有证据是否足以支撑某个调优结论。

## 范围

- 覆盖：证据采集、Kitex 特征帧解读、证据充分性判定、埋点开销判断。
- 不覆盖：具体调优动作、通用 Go 分析方法（`code-optimizer` 的 `go-pprof-analysis`）。

## 出处

- 采样入口与采样率要求：Go 官方 pprof 文档。
- 特征帧对应关系：目标版本的传输、派发、编解码实现位置。

## 命中验证与越界验证

- 命中：问"只有长尾抖动，先采什么"，应答"CPU + 锁/阻塞 + 堆的采集顺序与充分性判定"。
- 越界：问"并行度调到多少"，应答属于运行时参数，转 `go-runtime-tuning`。

## 权限与降级

- 环境与工具：Linux（aarch64/x86）；需要 Go 工具链（`go tool pprof`）、目标进程的 pprof 端点，以及可选的 `perf`/`strace`；应用侧采集不需要 root。
- 抓取 profile 需要目标进程的读取权限；无权限时降级为已归档证据，并在结论中标注来源与时间窗口。
- 采样有代价：限定抓取次数、时长与并发；观测与收益测量分开执行。

## 采集清单

| 证据 | 开启方式 | 用于 |
|---|---|---|
| CPU profile | 暴露 pprof 端点后抓取数秒 | 判断计算/系统调用/调度占比 |
| 堆 profile | 按分配空间与分配对象数各抓一次 | 定位每请求分配来源 |
| 锁 profile | **必须显式设置互斥锁采样率**，否则为空 | 定位连接获取、派发池争用 |
| 阻塞 profile | **必须显式设置阻塞采样率** | 定位 goroutine 阻塞来源 |
| GC 跟踪 | 打开 GC 跟踪环境变量 | GC 次数与停顿 |
| 系统调用计数 | 对进程做系统调用统计 | 判断是否系统调用主导 |
| 硬件计数 | 采集周期/指令/缓存未命中/上下文切换 | 判断 IPC 与访存 |

Kitex 服务端最小采集脚本见 `assets/server_main.go`（专题共享）（已内置 pprof、锁采样、阻塞采样开关）。

## 如何读 Kitex 特征帧

| 帧 | 含义 | 对应 skill |
|---|---|---|
| 系统调用（readv/sendmsg/epoll 等） | 网络收发与事件等待 | `kitex-transport-tuning` |
| 事件循环 / 派发池 | 每请求的 goroutine 派发 | `kitex-transport-tuning` |
| 连接获取/归还 | 独占连接池竞争 | `kitex-transport-tuning` |
| 编解码 | 序列化/反序列化 | `kitex-thrift-codec-selection` |
| 字符串/二进制解码、上下文链、元信息 | 每请求分配 | `kitex-allocation-hotspot-tuning` |
| 头部解析与压缩（gRPC） | 协议附加成本 | `kitex-transport-protocol-selection` |

## 证据充分性判定

满足以下条件才允许进入调优：

1. 目标热点在同一份证据的两次采样中稳定出现，且占比不可忽略。
2. 有对照：至少一次"改前/改后"或"开/关"的同口径数据。
3. 系统调用、锁、分配三类证据至少覆盖两类，避免单点误判。
4. 观测数据（profile/系统调用统计/硬件计数）与正式收益数据分开采集。

## 埋点开销的判断

不要默认"关掉埋点就更快"。埋点级别从关闭切到详细后，是否产生可测差异必须用同口径开/关对照确认；多数情况下差异落在噪声内，此时应保留可观测性，通过控制事件数量与采样来降开销。

## 注意事项

- 锁与阻塞采样默认关闭，不设置采样率会拿到空 profile。
- 抓 profile 本身会降速，不能与收益测量混在一起。
- 共享机器上先确认客户端不是瓶颈，再解读服务端数据。
- 只有单次采样、没有对照时，只能输出"怀疑方向"，不能输出结论。

## 输出

- 输出主导瓶颈类别、证据引用与充分性判定；证据不足时给出最小补采清单。

## 与相邻技能的边界

- 通用 Go profiling 方法在 `code-optimizer` 侧 `microservice/go-pprof-analysis`；本 skill 只负责 Kitex 特征帧与证据充分性判定。
- 证据不足时只能输出"怀疑方向"，不得进入调优；调优动作由各专项 skill 决定。
- 本 skill 可独立调用，不依赖 `WORKFLOW.md` 的中间产物。

## 参考

- 采集命令与解读示例：[`references/evidence-playbook.md`](references/evidence-playbook.md)
- 通用 Go profiling：`code-optimizer` 侧 `microservice/go-pprof-analysis`（按需）
