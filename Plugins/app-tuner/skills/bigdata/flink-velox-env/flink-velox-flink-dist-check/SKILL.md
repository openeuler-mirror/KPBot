---
name: flink-velox-flink-dist-check
description: >
  Flink + GlutenFlink（velox 向量化栈）所依赖的 Flink 发行版只读检查：定位安装目录（FLINK_HOME → PATH 上的 flink →
  /opt/flink → /usr/local/flink），stock 脚本齐全、lib 里只有一个 flink-dist jar、版本在 1.19 版本线（只比 major.minor，
  版本取 jar 文件名、缺失时回退 MANIFEST.MF）、conf 文件存在、REST 地址解析；逐项 ok/warn/fail + fix + 唯一 next_step。
  只扫不改：不装 Flink、不重启集群。触发词：Flink 装了没、Flink 版本、是不是 1.19、flink-dist、lib 里有几个 dist jar、
  FLINK_HOME 对不对。不负责：守护进程用的 Java → flink-velox-jdk-check；lib 里是 native 还是 GFV 构建 →
  flink-velox-lib-state-check（规划中）；"能不能编译 GFV 栈"的总裁决 → flink-velox-env-check。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink 发行版检查

GFV 栈以 `$FLINK_HOME` 里的 Flink 为宿主：velox4j 与 gluten-flink 的产物部署进它的 `lib/`，集群脚本从它的 `bin/` 起停。安装不全（少了 `bin/sql-client.sh`、没有 `flink-dist` jar）、lib 里混进两个版本的 `flink-dist` jar（Flink 自己的 `config.sh` 会拒绝启动）、或者装的根本不是 1.19 版本线——这些都会在编译部署或起集群时才炸。本 skill 只做一件事：**把 Flink 安装逐项核一遍——位置、文件、dist jar、版本线、conf、REST 地址**。它是 `flink-velox-env` 检查家族里"Flink 发行版"这一具体项：只读、不改动机器、不装 Flink、不重启集群。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 搭 GFV 环境之前或之后，确认 Flink 装对了没有、在不在 1.19 版本线
- `bin/config.sh` 报 `Multiple flink-dist*.jar found`、`flink: command not found`、起集群失败时先查安装
- 换机器、换容器、换了 `FLINK_HOME` 之后确认安装与 REST 地址
- 要知道本机 Flink 的精确版本（jar 文件名或 MANIFEST）

### 不适用（不要用本 skill）

- 指望它装 Flink、删多余 jar 或重启集群——`fix` 与 `next_step` 只是工单；重启须用户同意
- 守护进程实际用哪个 Java、`env.java.home` → `flink-velox-jdk-check`
- `lib/` 里是 native 还是 GFV 构建、部署齐不齐 → `flink-velox-lib-state-check`（规划中）
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`

## 检查项

| 条目 | 怎么判 |
|---|---|
| `home` | 依次在 `FLINK_HOME` → PATH 上 `flink` 的真实路径上两级 → `/opt/flink` → `/usr/local/flink` 找**有 `bin/flink`** 的目录；找不到为 fail |
| `files` | `bin/flink`、`bin/start-cluster.sh`、`bin/stop-cluster.sh`、`bin/sql-client.sh` 可执行，且 `lib/flink-dist*.jar` 存在；缺任一为 fail 并点名 |
| `dist-jars` | `lib/flink-dist*.jar` 多于一个为 fail（发行版 `bin/config.sh` 的 `constructFlinkClassPath` 遇多个即报 `Multiple flink-dist*.jar found` 并退出，1.19.2 实测） |
| `version` | 取文件名序第一个 `flink-dist*.jar`：文件名里的 `x.y.z`，缺失时读其 `META-INF/MANIFEST.MF` 的 `Implementation-Version`；读不到为 fail；只比 **major.minor = 1.19**，patch 不限；不在 1.19 线为 fail |
| `conf` | `conf/config.yaml` 或 `conf/flink-conf.yaml` 至少一个存在；都不在为 warn（发行版自带一个） |
| REST 地址 | `$FLINK_REST_URL` → conf `rest.address` / `rest.port` → `http://localhost:8081`，与家族其它 skill 同序，只解析不访问 |

## 执行步骤

### 步骤 1：扫描

```bash
python3 <skill_dir>/scripts/flink_dist_check.py [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `flink-dist  OK       Flink 1.19.2 at /usr/local/flink (via FLINK_HOME; version from lib/flink-dist-1.19.2.jar); REST http://localhost:8081`；缺了逐项 `FAIL <条目>: …` 并附 `fix:`；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/flink-dist-check.json`（留档规则见「环境约定」）。

退出码：**0** 没有 fail（warn 允许）；**1** 有 fail；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——按"装 Flink → 补齐文件 → 去掉多余 dist jar（需重启）→ 换到 1.19 版本线"的顺序给出唯一一个下一步。
2. `READY` 只说明安装完整且在 1.19 线上，**不代表集群已起、lib 里是哪种构建**：那两项分别在 `cluster` / `lib_state` 类检查（规划中）里。
3. `conf` 的 warn 不阻断但要转述；`rest_url` 只告诉你地址，是否应答由集群类检查回答。
4. 本 skill 不改任何东西：涉及删 jar 后重启的交给切换类 skill 并**征得用户同意**；装 Flink 由安装类 skill（规划中）执行，做完重扫直到 READY。

## 环境约定

- **只读**：只查文件与可执行位、读 jar 内 MANIFEST、读 conf 文本；不访问 REST、不执行任何 Flink 脚本；不写任何文件（`--out` 指定的除外）
- `ENV_CHECK_SYSROOT`（给所有绝对系统路径加前缀）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`flink`、`missing`、`next_step`、`error_message` 必须保留（下例缺 dist jar 且 conf 不存在，节选）：

```json
{
  "flink_velox_flink_dist_check_result": {
    "success": false,
    "checked_at": "2026-11-24T09:40:00+08:00",
    "status": "fail",
    "flink": {"path": "/opt/flink", "found_via": "default", "version": null, "on_1_19_line": null,
              "version_source": null, "dist_jars": [], "rest_url": "http://localhost:8081", "conf": {}},
    "items": [
      {"name": "home", "status": "ok", "detail": "Flink ? at /opt/flink (via default)"},
      {"name": "files", "status": "fail", "detail": "incomplete installation, missing lib/flink-dist-*.jar", "fix": "unpack the distribution again, or point FLINK_HOME at a complete one"},
      {"name": "version", "status": "fail", "detail": "cannot determine the Flink version: lib/flink-dist*.jar has no version in its name nor an Implementation-Version in its MANIFEST.MF", "fix": "install the official binary distribution flink-1.19.x-bin-scala_2.12.tgz and point FLINK_HOME at it"},
      {"name": "conf", "status": "warn", "detail": "neither conf/config.yaml nor conf/flink-conf.yaml exists — the stock distribution ships one"}
    ],
    "missing": ["files: incomplete installation, missing lib/flink-dist-*.jar", "version: cannot determine the Flink version: …"],
    "next_step": "unpack the Flink distribution again (or point FLINK_HOME at a complete one), then rerun",
    "error_message": "2 item(s) missing: files, version"
  }
}
```

- 原始观测 `flink`：`path` / `found_via`（FLINK_HOME / PATH / default）、`version`、`on_1_19_line`（true / false→null）、`version_source`（`lib/<jar>` 或 `lib/<jar> MANIFEST.MF`）、`dist_jars`（全部 dist jar 名，按名序）、`rest_url`、`conf`（`rest.address` / `rest.port` / `env.java.home` 三个键的存在子集）；`items` 是逐项判定（`name` / `status` / `detail`，fail 与部分 warn 另带 `fix`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `files: incomplete installation, …`
- 失败形态：`success: false`，`error_message` 形如 `2 item(s) missing: files, version`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-jdk-check`：守护进程与重启会选的 Java——它借本 skill同一套安装定位与 conf 读取，但不判版本线
- `flink-velox-lib-state-check`（规划中）：`lib/` 里是 native 还是 GFV 构建、运行依赖齐不齐
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 装 Flink、删多余 jar、重启集群：同族安装 / 切换类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
