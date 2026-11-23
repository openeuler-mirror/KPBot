---
name: kafka-deployment-benchmark
description: 部署或验证 Apache Kafka，运行健康检查、正确性验证与可复现的性能基准。适用于新建测试环境、检查已有实例和比较配置变更后的性能。
---

# Apache Kafka 部署与性能测试

先读 [部署与测试指南](references/guide.md)。按用户需求执行部署、最小验证或性能测试；支持已有实例直接进入验证。使用官方发行包与工具，操作步骤、命令和测试入口均在本 skill 内说明。

## 执行与复现

优先读取已有版本、拓扑与配置证据；已有实例直接验证。新部署按场景指南选择目标版本和架构，安装目录、端口、数据位置与资源限制从目标环境确定，不绑定特定主机。只读环境检测与变更动作分开：候选动作先以 JSON 列出 target、commands、preconditions、validation 和 rollback，再按用户已授权范围执行；只要求计划时不变更环境。

完成健康/正确性验证后再测性能。每次实验记录版本/commit 或镜像 digest、脱敏配置、客户端与服务端拓扑、CPU/内存限制、数据规模或消息参数、查询集/数据清单校验和、预热与缓存策略。保存准确命令、原始日志、退出码与各轮结果。失败保留证据并停止依赖步骤，不自动重复导入。

报告列出验证结果、每轮指标、失败项与配置差异；对比双方除声明变量外保持相同条件。不同计时单位、数据规模或投递语义不直接合并。性能测试使用有限轮次并检查容量，不将 smoke test 当作性能结论。

## 随附测试

Python 3.10+、Linux/Bash 和目标 Kafka 版本所需的 JDK，并安装场景指南指定的官方客户端。以下命令在 skill 根目录执行；相对路径均相对当前调用目录。脚本输出新结果目录，拒绝覆盖旧证据。Kafka 验证会创建测试 topic 并写入三条消息，在已授权测试环境中运行。

```text
python3 scripts/check_service.py --kafka-dir kafka --bootstrap localhost:9092 --output results/smoke-001
python3 cases/client-check/test_client_check.py
```

`check_service.py --help` 列出连接、客户端和超时参数。离线测试验证客户端调用、结果校验与失败处理；真实服务验证使用第一条命令并保留输出，二者分别报告。通过最小验证后再按指南扩大负载。

运行产物隔离测试需 Git：`python3 cases/output-isolation/test_output_isolation.py`。随附忽略规则覆盖示例结果、实验与源码目录；自定义输出位置需自行检查 Git 状态，凭据始终保存在工作树外或通过环境注入。

检查脚本退出码：0 表示验证通过，1 表示客户端调用或结果验证失败，2 表示命令参数错误或无法创建输出目录（包括目录已存在）；换新目录后重试，不覆盖旧证据。case.yaml 的入口为各 case 的 run.sh，会生成该 case 的 results/result.json，记录 success、测试数、失败数与错误数。
