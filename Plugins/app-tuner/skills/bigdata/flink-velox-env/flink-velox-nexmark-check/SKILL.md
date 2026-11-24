---
name: flink-velox-nexmark-check
description: >
  Flink + GlutenFlink（velox 向量化栈）跑 nexmark 基准所需两件东西的只读检查：Flink `lib/` 里的 nexmark-flink jar
  （nexmark 没有发布件，要从 github.com/nexmark/nexmark 源码构建）和可运行树（`bin/run_query.sh` + 查询/资源文件，
  按 NEXMARK_HOME → /usr/local/nexmark → /opt/nexmark 找）。两者都在才 READY，缺哪个给哪个的动作；lib 里多个 nexmark jar
  给 warn。只扫不改：不构建、不拷贝。触发词：nexmark 准备好了没、nexmark jar、nexmark 可运行树、NEXMARK_HOME、
  run_query.sh。不负责：跑 nexmark 基准测吞吐 → 基准类 skill（规划中）；nexmark 正确性校验 → 校验类 skill（规划中）；
  Flink 安装完整性 → flink-velox-flink-dist-check。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Nexmark 准备状态检查

nexmark 是 GFV 栈的吞吐基准口径，但它不是一条 `dnf install` 就能有的东西：`nexmark-flink` 没有发布件，要从 `github.com/nexmark/nexmark` 源码 `mvn -pl nexmark-flink -am package` 构建出 jar 拷进 Flink 的 `lib/`；跑查询还要一棵可运行树（`bin/run_query.sh` 与查询 / 资源文件，即 checkout 的 `nexmark-flink/src/main/resources`），放在 `$NEXMARK_HOME`、`/usr/local/nexmark` 或 `/opt/nexmark`。缺任一件，基准命令都在半途才报错。本 skill 只做一件事：**核对这两件在不在**。它是 `flink-velox-env` 检查家族里"nexmark 准备"这一具体项：只读、不改动机器、不构建、不拷贝。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 跑 nexmark 基准之前，确认 jar 与可运行树都到位
- `java -jar nexmark-flink*.jar` / `bin/run_query.sh` 报找不到类或找不到查询文件时，先看缺的是哪一件
- 换机器、换容器、换了 `NEXMARK_HOME` 后确认位置
- lib 里出现多个 nexmark jar（多次构建没清理）时点名

### 不适用（不要用本 skill）

- 指望它构建 nexmark、拷 jar 或铺可运行树——`fix` 与 `next_step` 只是工单，交给安装类 skill（审中 !323）
- 跑 nexmark 测吞吐、校验 nexmark 结果 → 基准 / 校验类 skill（规划中）；本 skill 不提交任何作业
- Flink 安装是否完整、版本线 → `flink-velox-flink-dist-check`
- 只有 nexmark 场景才需要 nexmark：不做基准时这两项缺失与 GFV 栈本身无关

## 检查项

| 条目 | 怎么判 |
|---|---|
| `flink` | Flink 安装按 FLINK_HOME → PATH 上的 flink → /opt/flink → /usr/local/flink 定位（须有 `bin/flink`）；找不到为 fail（jar 要去它的 `lib/` 里找） |
| `jar` | `lib/*nexmark*.jar`：有则 ok（取文件名序第一个），多于一个另给 warn；没有为 fail |
| `home` | 可运行树按 `$NEXMARK_HOME` → `/usr/local/nexmark` → `/opt/nexmark` 找**有可执行 `bin/run_query.sh`** 的目录；找到后再按清单核对完整性——`bin/run_query.sh`、`bin/config.sh`、`conf/{log4j.properties,nexmark.yaml,config.yaml}`、`queries/{ddl_gen,ddl_kafka,ddl_views,insert_kafka}.sql`、至少一个 `queries/q*.sql`（`run_query.sh` 会 `.` 进 `bin/config.sh`，查询要有 ddl 与配置才跑得起来）；齐了为 ok（注明来源）；缺项为 fail，`home_missing` 与 detail 里列出来（只复制了 bin/ 就中断的半棵树会被抓到）；找不到目录为 fail |

## 执行步骤

### 步骤 1：扫描

```bash
python3 <skill_dir>/scripts/nexmark_check.py [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `nexmark     OK       jar: nexmark-flink-0.3-SNAPSHOT.jar in lib; home: runnable tree at /opt/nexmark (via default)`；缺了逐项 `FAIL <条目>: …` 并附 `fix:`（构建命令 / 铺树命令）；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/nexmark-check.json`（留档规则见「环境约定」）。

退出码：**0** jar 与可运行树都在（warn 允许）；**1** 任一缺失或找不到 Flink 安装；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——缺什么就把构建 jar 与铺可运行树两个动作一次列全，交给安装类 skill（审中 !323）执行，做完重扫直到 READY。
2. 多个 nexmark jar 的 warn 不阻断，但类路径顺序决定实际加载哪个版本，要转述。
3. 本 skill 不构建、不拷贝、不提交作业：基准本身与结果校验属基准 / 校验类 skill（规划中）。

## 环境约定

- **只读**：只列 `lib/*nexmark*.jar` 文件名、查 `bin/run_query.sh` 可执行位与清单里各文件在不在；不读 jar、不运行 nexmark；不写任何文件（`--out` 指定的除外）
- `ENV_CHECK_SYSROOT`（给所有绝对系统路径加前缀）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`nexmark.jar`、`nexmark.home`、`missing`、`next_step`、`error_message` 必须保留（下例缺可运行树，节选）：

```json
{
  "flink_velox_nexmark_check_result": {
    "success": false,
    "checked_at": "2026-11-24T10:50:00+08:00",
    "status": "fail",
    "nexmark": {"flink_home": "/usr/local/flink", "found_via": "FLINK_HOME", "jar": "nexmark-flink-0.3-SNAPSHOT.jar",
                "jars": ["nexmark-flink-0.3-SNAPSHOT.jar"], "home": null, "home_via": null, "home_missing": []},
    "items": [
      {"name": "jar", "status": "ok", "detail": "nexmark-flink-0.3-SNAPSHOT.jar in lib"},
      {"name": "home", "status": "fail", "detail": "no runnable nexmark tree (bin/run_query.sh) under $NEXMARK_HOME, /usr/local/nexmark or /opt/nexmark",
       "fix": "copy nexmark-flink/src/main/resources of the nexmark checkout as the runnable tree and point NEXMARK_HOME at it"}
    ],
    "missing": ["home: no runnable nexmark tree (bin/run_query.sh) under …"],
    "next_step": "nexmark is not ready: lay out the runnable nexmark tree (nexmark-flink/src/main/resources) and point NEXMARK_HOME at it, then rerun",
    "error_message": "1 item(s) missing: home"
  }
}
```

- 原始观测 `nexmark`：`flink_home` / `found_via`、`jar`（文件名序第一个 nexmark jar，无则 null）、`jars`（全部）、`home`（可运行树目录，无则 null）、`home_via`（`$NEXMARK_HOME` / `default`）、`home_missing`（清单里缺的项；树完整或不存在时为空列表）；`items` 是逐项判定（`name` / `status` / `detail`，fail 另带 `fix`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `jar: no nexmark jar in …`
- 失败形态：`success: false`，`error_message` 形如 `2 item(s) missing: jar, home`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-flink-dist-check`：Flink 安装完整性与版本线——nexmark jar 只是放进它的 `lib/`
- `flink-velox-lib-state-check`：lib 里 GFV 构建的判定——nexmark jar 不参与 GFV 构建分类（名字不含 gluten/velox）
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 构建 nexmark、铺可运行树：同族安装类 skill（审中 !323）；跑 nexmark 吞吐基准与正确性校验：基准 / 校验类 skill（规划中）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
