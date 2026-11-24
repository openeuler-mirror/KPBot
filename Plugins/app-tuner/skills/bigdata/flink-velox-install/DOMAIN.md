# Flink + GlutenFlink 安装部署子域（bigdata 域内）

Flink + GlutenFlink（gluten-flink + velox4j + velox 向量化执行栈）环境的**动作类** skill 家族：
每一步都会改动机器，所以共用一个执行模型：`plan`（默认）只打印将执行的命令与变更，`apply --yes` 才执行
（幂等、失败即停、记录变更清单），`verify` 调 `flink-velox-env` 检查家族复查。它是检查家族各项 `fix` /
`next_step` 的执行者：检查只说"缺什么"，本子域负责"装上"。成员按"构建环境 → 运行时 → 编译部署 → 切栈"
的顺序互相衔接，分 PR 提交。

## 为什么是一个子域

动作类 skill 与只读检查的纪律不同（默认不动机器、显式同意、可回退），放在一个子域里统一评审与约束；
每个成员各自独立可装、可单独触发，又按上面的顺序前后衔接。

## 准入判据

新增成员须同时满足：

1. **plan 默认、apply 显式确认**：不带 `--yes` 的 apply 必须拒绝；plan 不写任何文件、不运行任何改动机器的命令；
2. **幂等与失败即停**：每步先探测、已满足则跳过；第一步失败即停并报告已做 / 未做，重跑安全；不自动删除用户已有的目录；
3. **可核对**：变更清单（装了什么、克隆了什么、写了什么、挪了什么）进 JSON；需 root 的步骤在非 root 下拒绝而非降级；回退路径文档化（备份目录、变更清单），不自动卸载。

| skill | 定位 |
|---|---|
| [`flink-velox-build-env-prepare`](flink-velox-build-env-prepare/SKILL.md) | 准备构建环境：克隆 velox / velox4j / gluten，dnf 装 28 个 openEuler 包（工具链、构建工具、C++ 库、JDK 17），按 velox setup 脚本装源码级 C++ 库进 /usr/local（脚本来源与 sha256 留档）；克隆先落独占临时目录、已有的目录一律不删，经 sudo 时克隆以原用户执行；verify 调 env 家族五个检查 |

## 其它成员（分 PR 提交，合入后移入上表）

- `flink-velox-runtime-install`：安装 Flink 运行时与 nexmark；审中 !323
- `flink-velox-stack-build`：编译并部署 GlutenFlink 构建；审中 !324
- `flink-velox-cluster-switch`：在 native 与 GlutenFlink 之间切换并重启集群；审中 !325

## 与其它域的边界

- "缺什么"的诊断 → `flink-velox-env` 检查子域（只读）；本子域只执行修复
- SQL 双跑等价性验证 → `flink-velox-e2e-verify`；nexmark 正确性校验 / 吞吐基准 → 校验 / 基准类 skill
- Flink 参数推荐与资源计算 → 上级 bigdata 域的框架层调优内容

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/bigdata/flink-velox-install/**`
