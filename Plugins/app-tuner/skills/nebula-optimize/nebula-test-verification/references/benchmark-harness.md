# 压测工具、场景与判读

## 1. 工具链

| 组件 | 版本（实测） | 说明 |
|------|-------------|------|
| NebulaGraph-Bench | v1.2.0 | 场景定义与编排（Python） |
| k6 | v0.33.0 | 压测引擎（Go） |
| xk6 + k6-plugin | xk6 v0.4.1 / k6-plugin v1.0.0 | Nebula 扩展 |
| nebula-importer | v3.1.0 | 数据导入 |
| LDBC SNB DataGen | v0.3.3 | 数据生成 |

工具身份要记录到报告：k6 版本、importer 版本、Bench 版本、数据集版本。

## 2. 五个场景

| 场景 | nGQL | CSV 数据源 | 单次工作量（SF10 实测） |
|------|------|-----------|------------------------|
| Go1Step | `GO 1 STEP FROM "{}" OVER KNOWS YIELD KNOWS.creationDate` | person.csv | ~11 行，~1ms |
| Go2Step | `GO 2 STEP FROM "{}" OVER KNOWS YIELD KNOWS.creationDate` | person.csv | ~6,390 行，~7ms |
| Go3Step | `GO 3 STEP FROM "{}" OVER KNOWS YIELD KNOWS.creationDate` | person.csv | ~223,145 行，~127ms |
| FindShortestPath | `FIND SHORTEST PATH FROM "{}" TO "{}" OVER * YIELD path AS p` | person_knows_person.csv | ~8ms |
| InsertPerson | `INSERT VERTEX Person(...) VALUES "vid":(...)` | person.csv | 单/批量写入 |

执行特征：

- Go N STEP 是逐跳串行 RPC：第 1 跳拿 11 个顶点，第 2 跳用 11 个起点再查，第 3 跳继续放大。
- 每次 RPC 之间 graphd worker 在等响应，不消耗 CPU；因此 Go2/Go3 的 graphd CPU 利用率
  可能很低，但延迟很高。
- FindShortestPath 是双向 BFS，graphd 侧维护 visited/路径容器，瓶颈常在 graphd。
- Insert 是写路径，graphd 解析 + storaged Raft/WAL/RocksDB；CPU 往往不饱和，延迟受 I/O 影响。

## 3. k6 并发模型

| 概念 | 说明 |
|------|------|
| VU | 虚拟用户，每个 VU 一个 goroutine + 独立 session |
| 循环 | 读 CSV → 发查询 → 等响应 → 记录指标 → 下一轮 |
| 固定并发 | `-u 100 -d 60s`：100 VU 持续 60 秒 |
| 阶梯 | `-s 10s:10 -s 30s:10 -s 10s:50`：0→10→10→50 |
| 指标 | `iterations/s`（QPS）、`checks`、`latency`、`responseTime`、`iteration_duration`、分位数 |

关系：

```
iteration_duration = responseTime + CSV/客户端处理时间
responseTime       = latency + 网络 + 客户端解码
```

服务端瓶颈看 `latency` 与 Nebula CPU；客户端瓶颈看 k6 CPU 与 `iteration_duration` 的差距。

## 4. 生成脚本

```bash
cd "${BENCH_HOME}"

# 单场景 dry-run，生成 output/<Scenario>.js
python3 run.py stress run -s ldbc_bench -a 127.0.0.1:${GRAPH_PORT_1} \
  -scenario go.Go1Step --dry-run --args='-u 200 -d 120s'

# 复制为每个 graphd 一份，并显式改写 init 地址（不能只靠行尾注释）
cp output/Go1Step.js output/Go1Step_g1.js
cp output/Go1Step.js output/Go1Step_g2.js
sed -i "s#nebulaPool.init(\"[^\"]*\"#nebulaPool.init(\"127.0.0.1:${GRAPH_PORT_1}\"#" output/Go1Step_g1.js
sed -i "s#nebulaPool.init(\"[^\"]*\"#nebulaPool.init(\"127.0.0.1:${GRAPH_PORT_2}\"#" output/Go1Step_g2.js

# 启动前校验：各脚本的 init 地址必须互不相同，否则会打回同一个 graphd
grep -h -o 'nebulaPool.init("[^"]*"' output/Go1Step_g*.js | sort -u
test "$(grep -h -o 'nebulaPool.init("[^"]*"' output/Go1Step_g*.js | sort -u | wc -l)" -eq 2 \
  || { echo "ERROR: k6 脚本 init 地址重复，检查 sed 改写" >&2; exit 1; }
```

注意：

- `configCSV()` / `configOutput()` 用绝对路径；不同 k6 进程工作目录不同，相对路径会找不到文件。
- 多 k6 同时启动时错开几秒，避免同时打开 CSV。
- 多个 k6 进程必须用 `--address 127.0.0.1:<不同端口>` 给各自的 REST API 分配互不相同
  的本机监听端口；`--no-usage-report` 只关闭用量上报，**不会**解决端口冲突。

## 5. 为什么用多个 k6 直连

| 方案 | 问题/结果 |
|------|-----------|
| nginx/LB 单 k6 → 多 graphd | 中间层 worker 争用，抵消多 graphd 收益 |
| 单 k6 + `__VU % 2` 分地址 | xk6 `nebulaPool.init()` 是全局单例，**首个完成 init 的 VU 的地址生效**（后续 VU 的 init 是空操作，直接返回已初始化的池） |
| 多 k6 进程各连一个 graphd | 无中间层，流量真正分散（实测 QPS 明显更高） |

做法：

```bash
# k6-g1 绑 NUMA2 前半，k6-g2 绑后半
numactl -C 192-239 -m 2 "${BENCH_HOME}/scripts/k6" run output/Go1Step_g1.js \
  -u 200 -d 120s --summary-trend-stats "min,avg,med,max,p(90),p(95),p(99)" \
  --address 127.0.0.1:6565 > /tmp/k6_g1.txt 2>&1 &
sleep 3
numactl -C 240-287 -m 2 "${BENCH_HOME}/scripts/k6" run output/Go1Step_g2.js \
  -u 200 -d 120s --summary-trend-stats "min,avg,med,max,p(90),p(95),p(99)" \
  --address 127.0.0.1:6566 > /tmp/k6_g2.txt 2>&1 &
wait
```

总 QPS = 各 k6 `iterations/s` 之和；成功率取各 k6 的最小值；p99 取最差 k6 或按需汇总。

## 6. 绑核与资源

典型布局（384 逻辑核）：

```
Nebula 进程：0-191（或按固定分区）
k6 客户端：192-383（每个 k6 24–48 核）
```

原则：

- 客户端与服务端分 NUMA，避免争抢。
- 每个 k6 进程绑独立区间；k6 总 CPU 接近区间上限说明客户端可能先瓶颈。
- 服务端进程按部署方案绑核/浮动；同一轮内不要改布局。
- 记录 `nproc`、`uptime`、其他租户进程；环境变化会让结果不可比。

## 7. VU 与时长

默认参考（每 k6 进程）：

| 场景 | 默认 VU | 默认时长 | 说明 |
|------|--------:|---------:|------|
| Go1Step | 100–200 | 60–120s | 轻查询，高 QPS |
| Go2Step | 100–400 | 60–120s | 返回行数大，注意客户端 |
| Go3Step | 16–100 | 60–120s | 重查询，VU 过高会排队 |
| FindShortestPath | 50–100 | 60–120s | BFS，延迟高 |
| InsertPerson | 50–200 | 60–120s | 写路径，测试后重置 |

VU 不是越高越好：超过服务端容量后 QPS 平台化、p99 暴涨，只增加排队。做容量曲线时按
VU 阶梯测，报告“饱和点”而不是只报一个最高值。

## 8. 客户端瓶颈识别

| 信号 | 判断 |
|------|------|
| k6 CPU 接近绑核上限 | 客户端先瓶颈 |
| `iteration_duration` 远大于 `responseTime` | CSV/JS 处理占主导 |
| Go2/Go3 返回行数巨大、QPS 锁死 | 客户端解码成本超过服务端 |
| 加 k6 进程后 QPS 上升 | 之前是客户端瓶颈 |
| 服务端 CPU 低、`ss` 连接不多 | 请求没打出去/客户端慢 |

对 Go2/Go3，限制最终结果行数要在语句**管道后**加 `| LIMIT 100`（GO 的逐跳 LIMIT 参数须是
与步数等长的列表，直接在语句尾跟整数值会报语义错误）：服务端遍历扇出不变，但客户端解码量
大幅下降，瓶颈回到服务端。报告中必须注明是否加 `LIMIT`，否则不同口径不可比。

## 9. 监控与证据

压测期间同步采集：

```bash
# CPU 分布
mpstat -P ALL 1 60 > /tmp/mpstat.txt
# 进程 CPU/内存（pidstat -p 多 PID 用逗号分隔；pidof 输出空格分隔须先转换）
pidstat -p "$(pidof nebula-graphd | tr ' ' ','),$(pidof nebula-storaged | tr ' ' ','),$(pidof nebula-metad | tr ' ' ',')" 1 60 > /tmp/pidstat.txt
# 连接数（端口从实例配置读取：graphd 默认 9669、storaged 默认 9779；旧 ':967[0-9]' 匹配不到 9669）
GP=$(grep -oP '(?<=^--port=)[0-9]+' "${CONF_DIR}/graphd.conf"   2>/dev/null | head -1); GP=${GP:-9669}
SP=$(grep -oP '(?<=^--port=)[0-9]+' "${CONF_DIR}/storaged.conf" 2>/dev/null | head -1); SP=${SP:-9779}
ss -tn | grep -E ":(${GP}|${SP})" | wc -l
# HTTP 统计（graphd/storaged）
curl -s http://127.0.0.1:19669/stats | head
curl -s http://127.0.0.1:19779/stats | head
```

记录：

- 服务端各进程 CPU、整机利用率、客户端 CPU；
- `latency`/`responseTime` 分位数；
- 成功率与失败样本；
- 连接数、磁盘 await/%util、内存；
- 若做热点分析，记录 perf/topdown 采集窗口（见 `nebula-hotspot-analysis`）。

## 10. 结果解析

k6 输出示例：

```
checks.........................: 100.00% ✓ 23,123,456 ✗ 0
iterations.....................: 11,581,646  95,618.5/s
latency.......................: avg=1.2ms med=1.26ms p(99)=2.62ms
responseTime...................: avg=1.4ms med=1.45ms p(99)=3.1ms
```

判读：

- `iterations/s` 是 QPS；多 k6 相加。
- `checks` 是成功率；读场景必须 100%。
- `latency` 是服务端处理时间；`responseTime` 含网络。
- 服务端延迟低但 QPS 不涨 → 客户端/队列；服务端 CPU 高且延迟涨 → 服务端饱和。
- 成功率下降优先查失败原因，不要用“重试后成功”掩盖。

## 11. 常见坑

| 问题 | 原因 | 解决 |
|------|------|------|
| Go1Step QPS 只有预期一半 | 单 k6 只连一个 graphd / LB 单点 | 多 k6 直连不同 graphd |
| Go2Step QPS 锁死 ~6K | 客户端解码瓶颈 | 加 `LIMIT` 或减少返回列 |
| 结果两轮差异大 | 数据未重置/预热/其他进程 | 重置备份、预热、固定环境 |
| `no such file` | CSV/output 相对路径 | 改绝对路径 |
| `port 6565 in use` | 多个 k6 REST API 端口冲突 | 每个 k6 用 `--address 127.0.0.1:<不同端口>`（如 6565/6566）；`--no-usage-report` 只关用量上报，不解决冲突 |
| INSERT 成功率 <100% | 重复 VID/写入冲突 | 确认数据/场景预期，不要误判为故障 |
| 测完发现数据变了 | INSERT/DELETE 写库 | 每轮前重置到同一备份 |
| 换二进制后数据打不开 | RocksDB 版本不兼容 | 用匹配的备份与引擎版本 |
