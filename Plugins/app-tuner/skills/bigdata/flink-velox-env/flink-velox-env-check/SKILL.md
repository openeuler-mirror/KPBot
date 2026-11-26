---
name: flink-velox-env-check
description: >
  Flink + GlutenFlink（velox 向量化栈）环境统筹检查：一条命令按 --goal build|native 依次调 flink-velox-env
  检查家族的 9 个子 skill（只读子进程、各自超时），把各项 ok/warn/fail 聚合进六个组（build_env / repos /
  java / flink / lib / cluster），按组优先级给唯一 next_step（工单指向安装家族对应的修复 skill）；
  goal=build 全查 9 项，goal=native 跳过构建侧 4 项（记 disabled）查其余 5 项（jdk 按 native 口径）；
  缺子 skill 时人读报告打 NOT VERIFIED、退出 1，不假装成功。触发词：环境统筹检查、flink-velox 环境
  就绪、环境还差什么、goal build、goal native、动手装环境之前先总查一遍。不负责：单项细节（直接跑
  对应子 skill：工具链→flink-velox-toolchain-check、构建工具→flink-velox-build-tools-check、C++ 依赖→
  flink-velox-cpp-deps-check、源码仓→flink-velox-source-repos-check、JDK→flink-velox-jdk-check、发行版→
  flink-velox-flink-dist-check、lib→flink-velox-lib-state-check、集群→flink-velox-cluster-check、nexmark→
  flink-velox-nexmark-check）；装任何东西 → 安装家族（flink-velox-build-env-prepare / runtime-install /
  stack-build / cluster-switch）；跑查询验证 → flink-velox-e2e-verify；跑吞吐 → flink-velox-performance-verify。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库；须 flink-velox-env 检查家族的 9 个子 skill 装在本 skill 旁（源码树同子域或安装后平铺均可），缺哪个报 NOT VERIFIED
metadata:
  skill_type: tool_assisted
  category: verification
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# Flink + GlutenFlink 环境统筹检查

「这台机器离目标还差什么」在动手之前就该有答案：`--goal build`（能编译并跑 GlutenFlink 栈）或 `--goal native`（只跑原生侧）。本 skill 只做一件事：**按目标把 flink-velox-env 检查家族的 9 个子 skill 各自委派一遍（只读子进程），把 items / status / missing 聚合成六个组，并按优先级给出唯一的 next_step**——它是各具体项检查的统筹层，自己不做任何探测，也不执行任何修复。分组、委派契约与 next_step 路由细节在 [orchestrator-table.md](references/orchestrator-table.md)。

## 术语

这套栈的官方叫法是 GlutenFlink；本 skill 自身没有 `gfv` 用户可见标识符（仅 goal=native 时向 lib-state 传 `--expect native`——goal 口径开关，不是构建判定值；自己的 JSON 里也不输出构建判定值），该缩写只会原样出现在它转述的子检查 detail 里（如 lib-state 对 lib 的 native / gfv / partial 判定），按子检查的既有契约保留。

## 使用场景

### 适用（应使用本 skill）

- 动手装 / 编译 / 切栈之前，先总查一遍：还差什么、第一个该修的是哪一项
- goal=build：搭 GlutenFlink 构建环境前后，看构建侧（工具链 / 源码仓 / JDK 17）与运行侧（Flink / lib / 集群）各自的现状
- goal=native：只跑原生侧前，确认 JDK 口径（8/11/17 均可）、Flink 安装、lib、集群、nexmark 这 5 项
- 装完一个修复 skill 回来重跑，看下一项工单（next_step 会前进到下一个未就绪的组）

### 不适用（不要用本 skill）

- 想看单项细节（哪几个包缺、哪个守护进程的 Java 不对）——直接跑对应子 skill，本 skill 的 groups 只聚合
- 指望它装包、克隆、编译、切栈、重启——next_step 是交给安装家族的工单，本 skill 只读不动
- 跑 SQL 双跑验证 → `flink-velox-e2e-verify`；跑吞吐基准 → `flink-velox-performance-verify`
- 第二层全链薄调度（检查 → 安装 → 验证 → 基准按目标串联）是另一个 skill 的职责；本 skill 是它的诊断入口

## goal 语义

| goal | 含义 | 检查范围 |
|---|---|---|
| `build` | 要在这台机器上编译并跑 GlutenFlink 栈 | 全部 9 项：构建侧 4 项（工具链、构建工具、C++ 依赖、源码仓）+ JDK 17 口径 + 运行侧 4 项（lib / 集群只告知现状，便于规划部署） |
| `native` | 只跑原生侧（不编译） | 构建侧 4 项记 `disabled`（detail 标 goal=native 不需要，不算 fail 也不算 missing）+ 其余 5 项（jdk 按 native 口径：8/11/17 均可） |

## 流程

1. `plan`（默认且唯一子命令；本 skill 只读，没有 apply/verify）：逐项委派子检查——子进程 `python3 <子脚本> --json`（每项默认 120s 超时，`--timeout` 覆盖；子检查在自有会话中运行，超时对**整个进程组** SIGKILL——其派生的 java/mvn/flink 孙进程一并收掉，不留孤儿），解析其首个 `*_result` 键取 success / items / missing / next_step；子脚本崩溃、超时或无输出 → 该项 `fail`（detail 记异常，next_step 给出**复用本次实际调用**的手跑命令——解释器 + 绝对路径 + 本次全部定位参数，复查落在同一套安装上）；子 skill 不在本 skill 旁 → 该项 `missing`，整体 **NOT VERIFIED** 退出 1（必需检查缺失≠成功）
2. 定位参数显式传入时透传给需要的子检查（`--flink-home` → flink-dist / jdk / cluster / lib-state / nexmark 的 FLINK_HOME；`--nexmark-home` → nexmark 的 NEXMARK_HOME；`--repos-dir` → source-repos 的 `--repos-dir` 与 cpp-deps 的 GFV_REPOS_DIR + `--velox-dir <repos-dir>/velox`——两个检查钉在同一 checkout，优先于任何 VELOX_HOME；`--goal` → jdk 的 `--goal`，且 goal=native 时给 lib-state 传 `--expect native`——原生目标跑在原生构建上，栈不匹配即 fail、工单回到切栈步骤，build 目标两侧都可、不传）。**显式路径即唯一目标**：`--flink-home` 缺 bin/flink、`--nexmark-home` 缺 bin/run_query.sh → 拒绝退出 2（stderr，先于任何子检查启动——不给回退 PATH/默认目录再凭另一套安装判 READY 的机会）；未传的留给各子检查自己的发现逻辑
3. 聚合成 `groups[]`（六组，build_env / repos / java / flink / lib / cluster，每子检查 status ∈ ok / warn / fail / disabled / missing）；按优先级 **build_env → flink → java → lib → cluster**（repos 与 build_env 同属构建准备层，紧随其后）取第一个有 fail / missing 的组：missing → 装 skill 的工单，fail → 该组内第一个 fail 项**自己的 next_step 原文**（指向安装家族对应 skill）；全绿 → "goal \<g\> is ready: all checks pass"。warn 如实进 groups 但不拦截 next_step

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`goal`、`groups`（每检查的 `status` / `detail` / `missing` / `next_step`）、`missing`、`next_step`、`error_message` 必须保留（下例 goal=build：build_env 组缺子 skill、flink 组一项失败，节选）：

```json
{
  "flink_velox_env_check_result": {
    "success": false,
    "command": "plan",
    "goal": "build",
    "checked_at": "2026-11-26T10:00:00+08:00",
    "flink_home": null,
    "nexmark_home": null,
    "repos_dir": null,
    "timeout": 120,
    "groups": [
      {"name": "build_env", "checks": [
        {"name": "toolchain", "skill": "flink-velox-toolchain-check", "status": "ok", "success": true,
         "detail": "ready: the compile toolchain is complete", "missing": [],
         "next_step": "ready: the compile toolchain is complete"},
        {"name": "cpp-deps", "skill": "flink-velox-cpp-deps-check", "status": "missing", "success": false,
         "detail": "flink-velox-cpp-deps-check is not installed next to this skill", "missing": [],
         "next_step": "install the missing check skill(s) (flink-velox-cpp-deps-check), then rerun python3 …/env_check.py --goal build"}
      ]},
      {"name": "flink", "checks": [
        {"name": "flink-dist", "skill": "flink-velox-flink-dist-check", "status": "fail", "success": false,
         "detail": "1 item(s) missing: version", "missing": ["version: Flink 1.17.1 is not on the 1.19 line …"],
         "next_step": "install the Flink 1.19.x runtime instead (this installation is 1.17.1), then rerun"}
      ]}
    ],
    "missing": ["version: Flink 1.17.1 is not on the 1.19 line …"],
    "next_step": "install the missing check skill(s) (flink-velox-cpp-deps-check), then rerun python3 …/env_check.py --goal build",
    "error_message": "1 check(s) failing; 1 check(s) not run (sibling skills not installed): flink-velox-cpp-deps-check"
  }
}
```

- 人读报告按组分行，末尾结论行 + `next:` 行：全绿 `READY`；有失败 `NOT READY: N check(s) failing`；有缺子 skill `NOT VERIFIED: N check(s) not run (sibling skills not installed): …`（两者可同时出现，NOT VERIFIED 是头条）
- `success`：全部执行的项 ok（warn 允许）且无 missing 才为 true；disabled 不算 fail 也不算 missing
- 建议命令一律带解释器与绝对路径（`python3 …/env_check.py --goal build`），照抄即可执行；手动复查命令**复用本次实际调用**（定位 env 前缀 + 全部定位参数），复查落在同一套安装上
- 检查崩溃：退出码 2，JSON 仍是完整契约形态（观测位空值），`error_message` 以 `tool aborted:` 开头；子检查打出不是对象的 `*_result` 同样中止——统筹层不替畸形的子结果编语义
- 退出码：**0** 全部执行的项 ok 且无 missing；**1** 有 fail 或有 missing；**2** 工具错误（`tool aborted:`）、`--out` 被拒、显式定位路径无效（`--flink-home` 缺 bin/flink / `--nexmark-home` 缺 bin/run_query.sh）或命令行非法

## 环境约定

- **只读**：除 `--out` 指定的文件外零写盘；不装包、不克隆、不写 lib、不起停集群
- 子检查定位：`ENV_PREPARE_ENV_FAMILY_DIR` 指向的家族目录优先（tier-A 桩用），否则按本 skill 目录旁的 `<check>/scripts/<check>_check.py`（源码树同子域平铺与安装后平布局同一相对路径）
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；非普通文件（fifo 等）即使 `--overwrite` 也拒绝；**校验发生在任何检查启动之前**

## 边界与相邻 skill

- 9 个子检查 skill（`flink-velox-env` 家族）：单项探测与判据的唯一事实来源，本 skill 只是它们的聚合层
- 安装 / 构建 / 切换家族：`flink-velox-build-env-prepare`（工具链、构建工具、C++ 依赖、源码仓、JDK）、`flink-velox-runtime-install`（Flink 发行版、nexmark）、`flink-velox-stack-build`（编译部署进 lib）、`flink-velox-cluster-switch`（切栈重启）——next_step 工单的接收方
- `flink-velox-e2e-verify` / `flink-velox-performance-verify`：环境就绪之后的验证与基准
- 第二层全链薄调度（规划中）：消费本 skill 的 next_step 作为委派依据
