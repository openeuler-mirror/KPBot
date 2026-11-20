# Go 微服务 A/B 验证 Playbook

本文件定义 `microservice-go-syncpool-optimization` 的测试步骤。所有轮次固定负载、固定预热、固定时长，单轮只变更一个参数；不记录或引用其它环境的历史性能数据。

## 1. 验证原则

- 单变量：每轮只改变一个开关或一个取值。
- 同负载：客户端、并发、请求分布、连接复用和时长保持一致。
- 同二进制：除工具链补丁和参数外，业务源码、编译参数、配置和绑核保持一致。
- 可回退：每轮执行前写好 `forward_cmd` 和 `reverse_cmd`。
- 内存优先：RSS 或 OOM 触发时立即停止，收益再高也不保留。

## 2. 基线准备

1. 固定压测客户端、并发数、请求分布、超时和重试策略。
2. 固定预热时长和测量时长；预热与测量分开记录。
3. 记录基线二进制 `go version -m`、sha256、启动参数、环境变量和部署形态。
4. 记录基线 QPS、P50/P99、错误率、RSS、GC 次数和 GC 暂停时间。
5. 基线连续跑两轮，确认波动范围后再进入变更轮。

## 3. 证据采集

```bash
# CPU 与分配热点
go tool pprof -top -nodecount=20 <cpu-profile>
go tool pprof -sample_index=alloc_space <heap-profile>
go tool pprof -sample_index=inuse_space <heap-profile>

# GC 行为
GODEBUG=gctrace=1 <service>

# 运行时内存
go tool pprof -tags <heap-profile>
```

采集时确保业务流量正在打满，空载 profile 不作为判据。

## 4. 池清单

- 扫描业务代码中的 `sync.Pool` 定义，记录池数量、对象类型、对象大小、`New` 函数和 `Get`/`Put` 位置。
- 用 `alloc_space` 确认池对象在分配热点中的占比。
- 确认对象是短生命周期临时对象；长生命周期大对象不进入本 skill。
- 记录 `GOMAXPROCS` 与容器 CPU limit，二者不一致时先修正再验证。

## 5. A/B 分组

| 轮次 | 变更 | 目的 |
|---|---|---|
| R0 | 基线 | 建立当前负载下的 QPS/P99/RSS/GC 基线 |
| R1 | `GODEBUG=poolretain=1` | 单独验证跨 GC 保留 |
| R2 | `GODEBUG=poolprealloc=N` | 单独验证预分配，N 从保守值开始 |
| R3 | `poolretain=1` + `poolprealloc=N` | 组合验证，标记为交互轮 |

- R1 与 R2 必须各自独立成轮，不能合并。
- R2 的 N 逐档上调时，每档单独记录。
- R3 只用于确认组合行为，不用于单独归因。

## 6. 指标采集

每轮记录：

- 吞吐：QPS。
- 延迟：P50、P99、最大值。
- 错误率与超时数。
- 内存：RSS 峰值、RSS 稳态、OOM 次数。
- GC：GC 次数、GC 暂停总时长、堆大小变化。
- 分配：`alloc_space`、`inuse_space`、首次 `Get` 的 `New` 调用次数。

## 7. 正确性验证

```bash
"$GOROOT/bin/go" test sync -run 'TestPool' -count=1
"$GOROOT/bin/go" test -race sync -run 'TestPool' -count=1
```

- `-race` 只用于功能验证，不参与性能测量。
- 服务 smoke test 覆盖核心接口和错误路径。
- 长稳测试覆盖至少一个完整 GC 周期和业务高峰形态。
- 观察 RSS 是否收敛；持续上涨时判定为内存泄漏风险并回退。

## 8. 判定

- 正向收益且 RSS 可控：保留，记录生效参数、配置位置和验证证据。
- 负收益或错误率上升：回退到上一轮有效配置。
- 回退时删除补丁构建根目录，并复核基线 `$GOROOT/bin/go` 的版本与 sha256，避免残留补丁工具链。
- 收益在噪声区且内存代价不可接受：回退。
- RSS 超过阈值、出现 OOM 或长稳不通过：立即回退，不再继续调参。
- 组合轮收益无法与单轮区分时，标记为交互结果，不重复归因。

## 9. 归档

每轮归档：

- `forward_cmd.txt` 与 `reverse_cmd.txt`。
- 压测原始输出和统计结果。
- `gctrace` 日志、heap profile、CPU profile。
- 变更前后环境变量和进程状态快照。
- 二进制 `go version -m` 与 sha256。
- 构建根目录路径、补丁 hash 与基线工具链 sha256。
- 轮次结论、停止原因和回退结果。

## 10. 常见失败

- 压测期间 GC 未触发：延长测量时长或提高分配速率，确保覆盖 GC 周期。
- profile 中没有池热点：确认压测走到目标代码路径。
- 新工具链构建或服务重编译使用了基线 `$GOROOT`：停止本轮，改用隔离构建根目录重新构建，避免污染基线。
- `poolprealloc` 调大后 RSS 冲高：按内存估算公式回退到上一档。
- `poolretain` 开启后 RSS 持续上涨：检查是否有池对象长期不 `Put` 或对象过大。
- 容器内 `GOMAXPROCS` 与 CPU limit 不一致：先修正运行配置，再验证参数。
