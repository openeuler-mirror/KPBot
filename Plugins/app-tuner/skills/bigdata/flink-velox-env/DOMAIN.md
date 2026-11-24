# Flink + GlutenFlink 环境检查子域（bigdata 域内）

Flink + GlutenFlink（gluten-flink + velox4j + velox 向量化执行栈）开发与验证环境的**检查类** skill 家族：
若干具体项检查子 skill 各自回答一个会单独提出的问题，一个统筹 skill 按目标（build / native）调度它们、
聚合出 ok/warn/fail 与唯一 next_step。所有成员只扫不改——不装包、不克隆、不写 lib、不起停集群；
修复动作是交给安装/构建/切换类 skill 的工单。

## 为什么是一个子域

环境检查的诉求天然分两层：「这台机器能不能编译 / 跑 GlutenFlink 栈」是统筹问题；「缺不缺 cmake」「Flink 版本对不对」
「集群加载的是哪套 jar」是各自独立、会单独提出的问题。拆成统筹 + 具体项两层后，每个具体项可单独安装、
单独评审、单独触发，统筹 skill 只负责按目标聚合并给出唯一下一步；新的检查项按准入判据追加，不必改统筹。

## 准入判据

新增成员须同时满足：

1. **只读**：不改动机器（不装包、不克隆、不写 lib、不起停集群）；输出 ok/warn/fail + 逐项 fix，fix 是工单不是动作；
2. **单一诉求**：回答一个会单独提出的问题（工具链 / 构建工具 / C++ 依赖 / JDK / Flink 发行版 / 源码仓 / lib 构建 / 集群 / nexmark），
   触发词与兄弟成员不重叠，负例互相指向；
3. **自带契约**：独立脚本（仅 Python 标准库）+ tier-A 契约用例 + 触发评估集；探测路径可经根前缀（sysroot / proc）整机造假，
   用例不触真机、不访问真实集群。

## 成员

| skill | 定位 |
|---|---|
| [`flink-velox-lib-state-check`](flink-velox-lib-state-check/SKILL.md) | lib 部署状态：按 jar 把 lib 判成 native / gfv（planner + runtime + loader + velox4j + 14 运行依赖）/ partial（判 fail 并点名缺件），多版本（含 14 个运行依赖）warn；`--expect native|gfv` 声明想要的构建 |
| [`flink-velox-cluster-check`](flink-velox-cluster-check/SKILL.md) | 集群状态：/proc 找归属本安装的守护进程（按 flink-dist 真实路径），加载的构建（native / gfv / mixed）、lib 是否在启动后改过、REST /overview（忽略代理）；不在跑 / 缺 JM 或 TM（TM 注册自别处为 warn）/ mixed / lib 改过（逐进程比对）判 fail；有作业在跑时重启类建议改为先等作业 |
| [`flink-velox-nexmark-check`](flink-velox-nexmark-check/SKILL.md) | nexmark 准备：lib 里的 nexmark-flink jar 与按清单完整的可运行树（bin/run_query.sh、bin/config.sh、conf、queries；NEXMARK_HOME → /usr/local/nexmark → /opt/nexmark）都在才 READY，缺哪个、树缺什么都列出来 |
| [`flink-velox-toolchain-check`](flink-velox-toolchain-check/SKILL.md) | 编译工具链：PATH 上找 git / cmake / gcc / g++ / mvn 并取版本；缺的给 openEuler 包名（g++ → gcc-c++、mvn → maven） |
| [`flink-velox-build-tools-check`](flink-velox-build-tools-check/SKILL.md) | 构建辅助工具：ninja / ccache / autoconf / automake / libtool / flex / bison / python3 / patchelf（ninja → ninja-build） |
| [`flink-velox-cpp-deps-check`](flink-velox-cpp-deps-check/SKILL.md) | C++ 依赖两层：13 个包管理器级库按头文件探测（给 -devel 包名）；源码级库清单解析自 velox checkout 自带 setup 脚本，按 /usr/local 探测 |
| [`flink-velox-jdk-check`](flink-velox-jdk-check/SKILL.md) | Java：JDK 17 在不在、JAVA_HOME 是否指向它、Flink 守护进程实际 Java 与重启会选的 Java（conf env.java.home → JAVA_HOME → PATH，env.java.home 只读 Flink 实际读的那份配置）；本安装的每个守护进程逐个判定；goal=build 要求 17，native 只接受 8 / 11 / 17 |
| [`flink-velox-flink-dist-check`](flink-velox-flink-dist-check/SKILL.md) | Flink 发行版：安装定位（`FLINK_HOME` 设了即唯一目标、不回退，否则 PATH / 默认目录）、stock 脚本齐全、lib 下递归只有一个 flink-dist jar（点前缀子目录也进，与 find 同范围）、版本在 1.19 版本线（只比 major.minor；REST 地址只取自 Flink 实际读的那份配置，jar 名 → MANIFEST 回退）、conf 存在、REST 地址解析 |
| [`flink-velox-source-repos-check`](flink-velox-source-repos-check/SKILL.md) | 源码仓：velox / velox4j / gluten 各自存在、标志文件齐全、经 git 读分支与 HEAD（远端地址脱敏后输出）并与基线分支对比（偏离只提示）；仓库根 --repos-dir → GFV_REPOS_DIR → 向上找 repos/，单仓可用 VELOX_HOME 等指定 |

## 其它成员（分 PR 提交，合入后移入上表）

- 规划中：统筹 `flink-velox-env-check`（按 goal 调度各项检查、聚合裁决与唯一 next_step；等各具体项检查合入后提交）

## 与其它域的边界

- 修复动作（克隆源码、装依赖、装 Flink 运行时、编译部署、切栈重启）→ 同族安装 / 构建 / 切换类 skill（审中 !322–!325：`build-env-prepare`、`runtime-install`、`stack-build`、`cluster-switch`，各成家族文件夹），不在本子域；
- SQL 双跑等价性验证 → `flink-velox-e2e-verify`（规划中，后续 PR）；nexmark 吞吐基准 → 基准类 skill（规划中）；
- Flink 参数推荐与资源计算 → 上级 bigdata 域的框架层调优内容。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/bigdata/flink-velox-env/**`
