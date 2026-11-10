# 分步操作手册（Runbook）

> 配套 SKILL.md 的执行细则。命令以 cube-bench CLI 为参照（v0.7.x）；使用前用
> `--help` 核对当前版本参数。

## 一轮标准流程

```text
确认单实例健康 → 清理上轮残留 → 启动本轮 bench → 确认收尾 → 归档报告 → 判读
```

### 1. 单实例健康确认（纪元锚点）

```sh
cube-bench -c 1 -n 3 -m create-only -o c1_health.json
```

- 延迟在健康锚点数量级内 → 继续；
- 显著偏高 → 先排环境（组件状态、内核参数、磁盘），不要带病测并发。

### 2. 清理上轮残留

```sh
# 优先用 bench 脚本自带清理模式（无参/0 参通常只清理不跑 bench）
bash <bench 清理入口> 0

# 残留自查（三个都应归零/接近零）
cubecli <实例列表命令> | wc -l          # 实例数
ip -o link show | grep -cE "^[0-9]+: 192\.168\.[0-9]+\.[0-9]+:"   # TAP 数（按实际命名规则）
ls <network-agent 状态目录> | wc -l      # 状态文件数
# TAP 匹配须带行首接口名锚定：宿主机物理网卡（如 enp*）的别名/描述里
# 也可能出现 192.168.x.x 字样，无锚定的 grep 会误计数；删除操作同理，勿伤物理网卡
```

### 3. 启动本轮

```sh
cube-bench -c 100 -n 100 -m create-only -o results/runN.json
```

- 逐轮分步执行，**不要**一条命令串"多轮 bench + 清理"；
- 每轮一个报告文件，按轮次命名归档。

### 4. 确认收尾

- 日志出现收尾标志（`Done!`/`Summary`/报告保存提示）才算结束；
- 或持有启动 PID 判断存活；
- **不要** `pgrep -f cube-bench`（自匹配误判）。

### 5. 判读

```sh
python3 <skill>/scripts/analyze_bench_results.py --dir results/ --output verdict.json
```

按 verdict 判读：

- **轮次聚合看 `rounds.groups`**（按 concurrency/total/mode/template 分组）：每组独立的
  n/median/CV/stability；**引用某组结论前必须确认该组 n ≥ 3**——n < 3 的组（单轮
  c1 锚点、模板 A/B 对照各 1 轮）stability 为 "insufficient rounds"，只能作为单轮
  参考，不得当作已验证结论；
- **形态看各文件 `shape` 字段**，走下表。

## 形态 → 下一步动作对照

| shape | 含义 | 下一步 |
|---|---|---|
| `uniform-lift` | 共享资源争用（min/max 同步抬高） | 找共享路径：内存带宽、缓存行、串行化组件；加核无效 |
| `staircase` | 串行化排队（max ≫ min 分层） | 数台阶估并发上限；定位锁/单点服务；查该组件是否在共享 cgroup |
| `quantized-tail` | 量化效应（窄带长尾聚簇） | 找周期值：长尾中心 ≈ 轮询周期 + 首查超时；检查探针/重试参数 |
| `mixed` | 多因素叠加 | 分解：先剥离量化长尾再看剩余分布形态 |

## 三用例执行序列（完整基准）

```sh
# 用例一：吞吐（c50n500）× 3 轮
for N in 1 2 3; do clean; cube-bench -c 50 -n 500 -m create-only -o results/tput_$N.json; done

# 用例二：延迟（c100n100）× 3 轮，官方口径看 Total Time
for N in 1 2 3; do clean; cube-bench -c 100 -n 100 -m create-only -o results/lat_$N.json; done

# 用例三：暂停/恢复（按对应 SDK 基准工具口径）× 3 轮
# 指标：pause_avg / resume_avg
```

（上述循环仅为序列示意；实际操作逐轮分步执行并确认清理，见 SKILL.md 执行纪律。）

## 常见故障对照

| 现象 | 判定 | 处理 |
|---|---|---|
| 报 `no more resource` | 容量耗尽（非性能退步） | 清理实例；评估单节点容量上限（数量级 ~600+） |
| 轮间波动巨大（CV > 10%） | 环境不稳 | 查同机负载、后台同步任务；稳定后重测 |
| 服务重启后 cubelet 起不来 | 残留 TAP 过多（历史缺陷） | 清理 TAP 后再起服务 |
| bench 卡住不结束 | 清理与 bench 串在同一条命令里被超时掐断 | 分步执行；确认 PID 状态后重跑该轮 |
| 新模板比老模板慢 | 模板构建自带页缓存污染（已知机制） | 参考模板新鲜度排查；逐出后再测 |

## 报告归档建议

```text
results/
├── c1_health.json        # 纪元锚点（每纪元至少一次）
├── tput_1..3.json        # 用例一
├── lat_1..3.json         # 用例二
└── verdict.json          # 判读输出
```

连同纪元标识（组件版本、内核参数快照、模板 ID）一起归档，保证结论可复查。
