# Kitex 证据采集与解读示例

## 1. 打开采样

```go
// 必须在采集前设置，否则锁/阻塞 profile 为空
runtime.SetMutexProfileFraction(1)
runtime.SetBlockProfileRate(10000)
```

服务端同时暴露 pprof 端点（`assets/server_main.go` 已内置）。

## 2. 采集

```bash
curl -s -o cpu.prof   "http://127.0.0.1:6060/debug/pprof/profile?seconds=10"
curl -s -o mutex.prof "http://127.0.0.1:6060/debug/pprof/mutex"
curl -s -o block.prof "http://127.0.0.1:6060/debug/pprof/block"
curl -s -o heap.prof  "http://127.0.0.1:6060/debug/pprof/heap?sample_index=alloc_space"
go tool pprof -top -nodecount=20 bin/server cpu.prof
```

系统侧（仅定性）：

```bash
strace -c -f -p <pid>                       # 数秒后中断，看系统调用次数
perf stat -p <pid> -e cycles,instructions,cache-misses,context-switches -- sleep 5
numastat -p <pid>                           # 远端内存占比
```

## 3. 解读路径

按"先分类、再定位、后对照"三步走：

1. **分类**：看 CPU profile 中系统调用、分配、锁、派发、调度各自占比，确定主导类别。
2. **定位**：进入主导类别对应的帧，确认具体函数与调用链；锁与阻塞要到调用链层面看是谁在等待谁。
3. **对照**：对怀疑点做一次开/关或改前/改后采集，确认占比随改动变化。

示例判断：

- 系统调用占比最高 → 属于收发路径主导，先去传输层，不要先动编解码。
- 锁延迟集中在请求派发与连接处理路径 → 属于框架调度，不是业务临界区。
- 分配集中在字符串/二进制解码与元信息 → 先削分配，再考虑 GC 参数。

## 4. 常见误读

| 现象 | 误读 | 正确解读 |
|---|---|---|
| 锁 profile 为空 | 认为没有锁争用 | 采样率没开 |
| CPU 里系统调用占比高 | 认为是"框架慢" | 是内核收发路径成本，先调传输层 |
| 单次 profile 出现小热点 | 据此做改造 | 占比不可忽略且在两次采样中稳定才可作结论 |
| 观测时 QPS 下降 | 认为改动无效 | 观测器本身降速，必须分开测 |
| 堆 profile 只看分配空间 | 忽略小对象高频 | 同时看分配对象数 |

## 5. 证据充分性自检

- [ ] 目标热点在两次采样中稳定出现、占比不可忽略
- [ ] 有开/关或改前/改后同口径对照
- [ ] 系统调用、锁、分配至少覆盖两类
- [ ] 观测与收益测量分开
- [ ] 已确认客户端不是瓶颈
