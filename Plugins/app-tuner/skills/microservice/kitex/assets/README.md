# Kitex Demo 资产

供本专题各 skill 复用的最小 demo。**测量口径与判定规则以 `microservice-ab-testing` 为准**，这里只提供 Kitex 侧的可执行载体。

| 文件 | 用途 |
|---|---|
| `echo.thrift` | 最小 IDL：string / i64 / list\<i64\> / payload |
| `handler.go` | 回显 handler（只测框架，不测业务） |
| `server_main.go` | 带开关的 server：`-pollers -mux -frugal -stats -pprof -mutexprof -blockprof` |
| `bench_main.go` | 压测客户端：`-c -d -warmup -size -nums -mux -ttheader -frugal -stats -o` |
| `run_one.sh` | 单配置运行器：绑核 + 启动 + 计 CPU/RSS/GC + 输出 JSON |
| `env.sh` | 本地 Go 环境：按 `GO_BIN` / `GOROOT` / PATH 解析工具链，不写死路径 |

## 快速开始

```bash
# 任选一种让 env.sh 找到工具链：
#   GO_BIN=/path/to/go/bin . assets/env.sh
#   GOROOT=/path/to/go     . assets/env.sh
#   . assets/env.sh        # go 已在 PATH 上
GO_BIN=/path/to/go/bin . assets/env.sh
cp assets/echo.thrift .
kitex -module demo -service echo -thrift frugal_tag ./echo.thrift
cp assets/handler.go .
cp assets/server_main.go main.go
mkdir -p cmd/bench && cp assets/bench_main.go cmd/bench/main.go
cp assets/run_one.sh . && chmod +x run_one.sh
go mod tidy
go build -o bin/server . && go build -o bin/bench ./cmd/bench

# 偶数核 = 物理核，避开同核的 SMT 兄弟
SC=$(seq 0 2 62 | paste -sd,)
CC=$(seq 64 2 94 | paste -sd,)
bash run_one.sh base    "-pollers 0"      "-c 256 -d 5s -warmup 2s" $SC $CC 32 100 8888
bash run_one.sh poller8 "-pollers 8"      "-c 256 -d 5s -warmup 2s" $SC $CC 32 100 8888
bash run_one.sh mux4    "-pollers 8 -mux" "-c 256 -d 5s -warmup 2s -mux 4" $SC $CC 32 100 8888
```

## 约定

- `run_one.sh` 需复制到工程根目录执行（它按脚本所在目录找 `bin/`）；用 `bash run_one.sh` 调用可避免执行位丢失。
- 每配置至少 3 次取中位；客户端 CPU 使用率应低于 60%。
- 结果 JSON 中 `valid` 为假表示本轮不可用；`aborted` 表示早停、`samples_capped` 表示样本达到上限，这两类轮次不用于收益对比。
- 同名重跑会先删除上一轮结果文件；脚本返回本轮 bench 的退出码，失败时非零。
- pprof/strace/perf 只用于定性，正式对比必须在无观测器时跑。
- 远端执行统一走 `remote-runner`，不要在本文档里拼 SSH。
