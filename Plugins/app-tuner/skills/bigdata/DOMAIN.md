# 大数据域（app-tuner 侧）

大数据框架与服务的部署、验证和性能调优。已落地的独立技能与子域见成员表；
现有 Spark / Flink 知识仍内嵌于 `kpbot-app-tuner` 主框架，涵盖 ARM（鲲鹏）参数推荐、资源计算与基准执行协议。

## 独立成员

| 成员 | 定位 |
|---|---|
| [`doris-deployment-benchmark`](doris-deployment-benchmark/SKILL.md) | 部署、健康检查、正确性验证与性能基准 |
| [`flink-velox-env/`](flink-velox-env/DOMAIN.md) | 子域：Flink + GlutenFlink（velox 向量化栈）环境检查家族——具体项检查子 skill 各自独立安装与触发，统筹 skill 按目标（build / native）聚合并给出唯一 next_step；只扫不改，是本域安装/构建/验证类 skill 的共同前置裁决点，详见其 DOMAIN.md |

## 现有 Spark / Flink 知识

除上述独立成员覆盖的内容外，Spark / Flink 等大数据相关内容仍位于 `kpbot-app-tuner` 主框架的子技能里，
不随 `install.sh` 安装（`subskills/` 不单独安装），也没有独立的 review 归属，具体资源如下：

| 位置 | 内容 |
|---|---|
| [`subskills/application-config-optimization/references/bigdata-playbook.md`](../kpbot-app-tuner/subskills/application-config-optimization/references/bigdata-playbook.md) | 18 KB 方法正文：识别条件与环境自动检测、**Spark 参数推荐（ARM）**、**Flink 参数推荐（ARM）**（`taskmanager.memory.process.size`、JobManager/TaskManager 容器内存计算公式）、适用条件与扩展性、Benchmark 执行流程（清理残留进程 / 端口占用 / 僵尸 TaskManager / 集群状态校验）、常见问题、Spark on YARN 资源自动计算 |
| `subskills/application-config-optimization/scripts/` | `apply_spark_config.sh`、`apply_flink_config.sh`（推荐器模式：只输出候选命令 JSON，不直接改环境）、`run_tpcds_benchmark.sh`、`start_tm.sh`、`cleanup_benchmark_env.sh` |
| `subskills/application-config-optimization/SKILL.md`「大数据框架专项承接」 | 承接路由：`workload_type` 命中 spark/flink 或检测到 `spark-defaults.conf` / `flink-conf.yaml` 时引用上述 playbook |

也就是说：**方法在、脚本在、benchmark 协议在，缺的是一份能独立安装、能被按域评审、能被别的 skill 引用的入口。**

## 准入判据

新增成员须同时满足：

1. 结论落在**框架层**（Spark/Flink/计算引擎的配置、资源模型、shuffle/状态后端、并行度与内存配比），
   不是单机 OS 层（OS/BIOS/绑核 → app-tuner 其他域）也不是业务 SQL 改写；
2. 带**可复现的 benchmark 口径**（数据集、查询集如 TPC-DS 的规模因子、并发、SLA 指标）；
3. 有环境检测与候选命令输出的分离（推荐器模式：先出 JSON 候选动作，再由主框架安全门控执行）。

## 成员

| 成员 | 定位 |
|---|---|
| [`flink-velox-env/`](flink-velox-env/DOMAIN.md) | 子域：Flink + GlutenFlink（velox 向量化栈）环境检查家族——具体项检查子 skill 各自独立安装与触发，统筹 skill 按目标（build / native）聚合并给出唯一 next_step；只扫不改，是本域安装/构建/验证类 skill 的共同前置裁决点，详见其 DOMAIN.md |
| [`flink-velox-install/`](flink-velox-install/DOMAIN.md) | 子域：Flink + GlutenFlink 环境的安装部署家族（动作类，`plan` 默认只打印、`apply --yes` 才执行、`verify` 复查）——构建环境准备、Flink 运行时与 nexmark 安装、GlutenFlink 构建编译部署、切栈重启，成员分 PR 提交；是 `flink-velox-env` 检查家族各项修复动作的执行者，详见其 DOMAIN.md |

## 与其它域的边界

- **算子 / 编译器层**（Spark 的代码生成、JVM JIT、SIMD 库）→ `code-optimizer` 侧对应 skill，不在本域；
- **单机系统层**（NUMA、IRQ、页缓存、磁盘调度）→ `code-optimizer/skills/storage/` 或 app-tuner 其他域；
- **分布式存储客户端**（HDFS/Ceph 客户端参数）→ `code-optimizer/skills/storage/`，本域只管计算框架侧；
- **容器与沙箱**（框架跑在 microVM/AgentENV 里）→ `agentic-infra/`。

## 候选 Spark / Flink 成员（待原作者确认后抽取）

`bigdata-framework-tuning`：把 `bigdata-playbook.md` + 5 个脚本抽成独立 skill，母技能改为指针。
属于**新写 skill**而非搬移，需要原作者确认内容归属，故未随本 PR 落地。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/bigdata/**`
