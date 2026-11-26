---
name: clickhouse-deployment-benchmark
description: 部署或验证 ClickHouse，运行连接检查、正确性验证与可复现的性能基准。适用于新建测试环境、检查已有实例和比较配置变更后的性能。
---

# ClickHouse 部署与性能测试

先读 [部署与测试指南](references/guide.md)。按用户需求执行部署、最小验证或性能测试；支持已有实例直接进入验证。使用官方发行包与工具，操作步骤、命令和测试入口均在本 skill 内说明。

## 执行与复现

优先读取已有版本、拓扑与配置证据；已有实例直接验证。新部署按场景指南选择目标版本和架构，安装目录、端口、数据位置与资源限制从目标环境确定，不绑定特定主机。只读环境检测与变更动作分开：候选动作先以 JSON 列出 target、commands、preconditions、validation 和 rollback，再按用户已授权范围执行；只要求计划时不变更环境。

完成连接与 SQL 正确性验证，并另行核对目标集群状态后再测性能。每次实验记录版本/commit 或镜像 digest、脱敏配置、客户端与服务端拓扑、CPU/内存限制、数据规模（如 TPC-H SF 与生成参数）、查询集/数据清单校验和、预热与缓存策略。保存准确命令、原始日志、退出码与各轮结果。失败保留证据并停止依赖步骤，不自动重复导入。

报告列出验证结果、每轮指标、失败项与配置差异；对比双方除声明变量外保持相同条件。不同计时单位或数据规模不直接合并。性能测试使用有限轮次并检查容量，不将 smoke test 当作性能结论。

## 随附测试

Python 3.10+，Linux 上运行官方客户端，并安装场景指南指定的官方客户端。以下命令在 skill 根目录执行；相对路径均相对当前调用目录。脚本输出新结果目录，拒绝覆盖旧证据。验证脚本仅执行只读 SQL，认证使用客户端配置。

```text
python3 scripts/check_service.py --host localhost --port 9000 --output results/smoke-001
python3 cases/client-check/test_client_check.py
python3 cases/benchmark-rounds/test_benchmark_rounds.py
```

单二进制安装使用 `--client experiment/clickhouse --single-binary`；experiment/clickhouse 按实际相对位置填写。`check_service.py --help` 列出连接、客户端和超时参数。认证用户由命令行 `--user` 显式指定时才传入客户端；省略 `--user` 时使用 `--config` 客户端配置中的用户，两者都缺省时由客户端内置默认用户认证，脚本不会向命令行注入用户名。离线测试验证客户端调用、结果校验与失败处理；真实服务验证使用第一条命令并保留输出，二者分别报告。通过最小验证后再按指南扩大负载。基准轮次用 `bash scripts/run_benchmark_rounds.sh` 写入新建目录，分别保留预热与各测量轮次日志，目录已存在时拒绝覆盖；显式使用 `bash` 调用，权限为 0644 的复制/安装布局也可用。单条查询使用客户端 `--query`，多条查询使用脚本 `--query-file FILE`，二者互斥且不支持继承标准输入；脚本在每轮重新打开查询文件，并要求正数 `--iterations` 或 `--timelimit`。输入、文件或有限终止条件不合法时在启动客户端前以退出码 2 失败，不创建结果目录。

最小脚本的 `passed` 只表示目标连接及三条只读 SQL 通过，不验证集群所有节点、副本存活或复制延迟。集群测试前须按目标拓扑另行检查 `system.clusters` 与各节点的 `system.replicas`；不能从此脚本的结果推断集群健康。

运行产物隔离测试需 Git：`python3 cases/output-isolation/test_output_isolation.py`。随附忽略规则覆盖示例结果、实验与源码目录；自定义输出位置需自行检查 Git 状态，凭据始终保存在工作树外或通过环境注入。

检查脚本退出码：0 表示验证通过，1 表示客户端调用或结果验证失败，2 表示命令参数错误或无法创建输出目录（包括目录已存在）；换新目录后重试，不覆盖旧证据。case.yaml 的入口为各 case 的 run.sh，会生成该 case 的 results/result.json，记录 success、测试数、失败数与错误数。
