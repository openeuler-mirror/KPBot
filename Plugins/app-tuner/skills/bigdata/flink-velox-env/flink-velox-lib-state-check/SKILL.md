---
name: flink-velox-lib-state-check
description: >
  Flink + GlutenFlink（velox 向量化栈）在 `$FLINK_HOME/lib` 里部署状态的只读检查：按 jar 把 lib 判成三种构建之一——
  native（没有任何 gluten/velox jar）、gfv（planner + runtime + loader + velox4j + 14 个运行依赖齐全）、partial（只有一部分，
  类加载会在半途炸）；同一 artifact 多版本并存给 warn；`--expect native|gfv` 声明想要的构建，其它判 fail 并给到达它的动作。
  逐项 ok/warn/fail + fix + 唯一 next_step。只扫不改：不挪 jar、不重启集群。触发词：lib 里是 native 还是 GFV、
  部署齐不齐、gluten jar 在不在、多版本并存、partial。不负责：集群实际加载了哪套 → flink-velox-cluster-check；
  编译并部署进 lib → 构建部署类 skill（规划中）；"能不能编译 GFV 栈"的总裁决 → flink-velox-env-check。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink lib 部署状态检查

GFV 栈的产物部署在 `$FLINK_HOME/lib`：gluten-flink 的 planner / runtime / loader 三个 jar、velox4j，以及构建脚本一并拷进去的 14 个运行依赖（guava、arrow 系列、jackson 系列等）。lib 里**没有**这些 jar 就是 native 构建；**全有**是 GFV 构建；**只有一部分**是最坏的状态——作业提交后类加载在半途失败，报错离"少拷了一个 jar"很远。本 skill 只做一件事：**按 jar 名把 lib 判成三种构建之一，缺的点名，多版本的提示**。它是 `flink-velox-env` 检查家族里"lib 部署状态"这一具体项：只读、不改动机器、不挪 jar、不重启集群。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 编译部署之后，确认 GFV 构建完整地进了 lib（没有漏掉运行依赖）
- 作业报 `ClassNotFoundException` / `NoClassDefFoundError` 时先看 lib 是不是 partial
- 跑 native 侧之前确认 lib 里没有 gluten/velox jar（`--expect native`）
- lib 里出现同一 artifact 的多个版本（多次部署没清理）时点名多余 jar

### 不适用（不要用本 skill）

- 指望它挪 jar、清理多版本或重启集群——`fix` 与 `next_step` 只是工单，重启须用户同意
- 集群进程**实际加载**了哪套、lib 改了之后集群是否还在跑旧的 → `flink-velox-cluster-check`
- Flink 安装本身是否完整、版本线 → `flink-velox-flink-dist-check`
- 编译 velox4j / gluten-flink 并部署进 lib → 构建部署类 skill（规划中）
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`

## 期望（--expect）

| 取值 | 含义 | 判 fail 的情形 |
|---|---|---|
| `any`（默认） | 只问 lib 里是哪套、齐不齐 | partial；找不到安装 |
| `native` | 要跑 native 侧：lib 里不能有 gluten/velox jar | gfv、partial |
| `gfv` | 要跑 GFV 构建：lib 里必须是完整 GFV 集 | native、partial |

## 检查项

| 条目 | 怎么判 |
|---|---|
| `home` / `lib` | Flink 安装按 FLINK_HOME → PATH 上的 flink → /opt/flink → /usr/local/flink 定位（须有 `bin/flink`）；安装没有 `lib/` 目录为 fail |
| `build` | lib 里 `*.jar`：无 gluten/velox jar → native；`gluten-flink-planner-` / `gluten-flink-runtime-` / `gluten-flink-loader-` 各至少一个、`velox4j-` 至少一个、14 个运行依赖各至少一个 → gfv；否则 partial，`missing_parts` 点名缺的 artifact（velox4j 排最前） |
| `duplicates` | 同一 artifact 多个版本（如 `gluten-flink-runtime-1.8.0` 与 `1.9.0`）为 warn，列出全部 jar |

## 执行步骤

### 步骤 1：定期望、扫描

```bash
python3 <skill_dir>/scripts/lib_state_check.py [--expect any|native|gfv] [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `lib-state   OK       gfv build in /usr/local/flink/lib: complete GFV set: planner runtime loader + velox4j + 14 runtime deps`；不符期望或缺件逐项 `FAIL build: …` 并附 `fix:`；多版本 `WARN duplicates: …`；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/lib-state-check.json`（留档规则见「环境约定」）。

退出码：**0** lib 持有期望的构建且不是 partial（warn 允许）；**1** 不符期望、partial 或找不到安装；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——按"partial 补齐或清空 → 不符期望则切换 → 清理多版本"的顺序给出唯一一个下一步；每条涉及重启的都注明须用户同意。
2. `READY` 只说明 lib 里的内容符合期望，**不代表集群加载的就是它**：改了 jar 没重启时集群还在跑旧的，由 `flink-velox-cluster-check` 回答。
3. 多版本的 warn 不阻断，但必须转述：类路径上先后顺序决定实际加载哪个版本。
4. 本 skill 不挪 jar、不重启：把 `next:` 交给构建部署 / 切换类 skill（规划中）并**征得用户同意**，做完重扫直到 READY。

## 环境约定

- **只读**：只列 `lib/*.jar` 的文件名；不读 jar 内容、不碰集群；不写任何文件（`--out` 指定的除外）
- `ENV_CHECK_SYSROOT`（给所有绝对系统路径加前缀）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`expect`、`lib_state.build`、`lib_state.missing_parts`、`missing`、`next_step`、`error_message` 必须保留（下例 partial，节选）：

```json
{
  "flink_velox_lib_state_check_result": {
    "success": false,
    "checked_at": "2026-11-24T10:30:00+08:00",
    "expect": "any",
    "status": "fail",
    "lib_state": {
      "build": "partial", "lib": "/usr/local/flink/lib", "flink_home": "/usr/local/flink", "found_via": "FLINK_HOME",
      "gfv_jars": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar", "gluten-flink-planner-1.9.0-SNAPSHOT.jar", "gluten-flink-runtime-1.9.0-SNAPSHOT.jar", "velox4j-0.1.0-SNAPSHOT.jar"],
      "gluten_jars": {"gluten-flink-planner": ["gluten-flink-planner-1.9.0-SNAPSHOT.jar"], "gluten-flink-runtime": ["gluten-flink-runtime-1.9.0-SNAPSHOT.jar"], "gluten-flink-loader": ["gluten-flink-loader-1.9.0-SNAPSHOT.jar"]},
      "velox4j_jars": ["velox4j-0.1.0-SNAPSHOT.jar"],
      "runtime_deps": {"expected": 14, "missing": ["jackson-core", "jackson-databind", "jackson-datatype-jdk8", "jackson-datatype-jsr310"]},
      "duplicates": {}, "missing_parts": ["jackson-core", "jackson-databind", "jackson-datatype-jdk8", "jackson-datatype-jsr310"]
    },
    "items": [
      {"name": "build", "status": "fail", "detail": "an incomplete GFV set is in lib: missing jackson-core, jackson-databind, jackson-datatype-jdk8, jackson-datatype-jsr310 (4 gluten/velox jar(s): …) — half a stack fails at class-loading", "fix": "deploy the complete GFV build into lib, or remove every gluten/velox jar, then restart the cluster (a restart needs the user consent)"}
    ],
    "missing": ["build: an incomplete GFV set is in lib: …"],
    "next_step": "deploy the complete GFV build into lib (missing jackson-core, jackson-databind, jackson-datatype-jdk8, jackson-datatype-jsr310) or remove every gluten/velox jar, then restart the cluster (a restart needs the user consent), then rerun",
    "error_message": "1 item(s) missing: build"
  }
}
```

- 原始观测 `lib_state`：`build`（native / gfv / partial / null）、`lib`、`flink_home` / `found_via`、`gfv_jars`（所有 gluten/velox jar，按名序）、`gluten_jars`（三个 artifact 各自的 jar）、`velox4j_jars`、`runtime_deps`（`expected` 14 / `missing` artifactId 列表）、`duplicates`（artifact → jar 列表）、`missing_parts`；`items` 是逐项判定（`name` / `status` / `detail`，fail 另带 `fix`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `build: an incomplete GFV set …`
- 失败形态：`success: false`，`error_message` 形如 `1 item(s) missing: build`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-cluster-check`：集群守护进程实际加载了哪套、lib 与集群是否一致——它消费本 skill 同一套 lib 判定
- `flink-velox-flink-dist-check`：Flink 安装完整性与版本线（lib 里只有一个 `flink-dist` jar 等）
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 编译并部署进 lib、挪 jar、重启集群：同族构建部署 / 切换类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
