---
name: flink-velox-runtime-install
description: >
  安装 Flink + GlutenFlink（velox 向量化栈）所依赖的 Flink 运行时与 nexmark 基准件——官方 Flink 1.19.x 二进制包
  （镜像下载，sha512 恒取 archive.apache.org 校验）解压到 /opt/flink-<版本> + /opt/flink 软链 + 全注释的 conf 补最小配置；
  克隆 nexmark 源码、mvn 构建 nexmark-flink jar 放进 Flink lib、铺 /opt/nexmark 可运行树。三段式：`plan`（默认）只打印、
  `apply --yes` 才执行（幂等、失败即停、变更清单；/opt 不可写时非 root 拒绝）；`verify` 调 flink-dist-check 与 nexmark-check。
  触发词：安装 Flink、装 Flink 运行时、下载 flink tgz、/opt/flink、装 nexmark、nexmark jar、run_query.sh、NEXMARK_HOME。
  不负责：查装没装 → flink-velox-flink-dist-check / nexmark-check；装构建依赖 → flink-velox-build-env-prepare；
  编译部署 GFV 构建 → flink-velox-stack-build。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需 curl、tar、sha512sum；nexmark 需 git、mvn、JDK 8|17；只接受 1.19 版本线
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# Flink 运行时与 nexmark 安装

GFV 栈跑在官方 Flink 发行版上：velox4j 与 gluten-flink 的产物部署进它的 `lib/`，集群从它的 `bin/` 起停。发行版是 Apache 的 tgz（国内从华为云镜像取，校验文件只从 archive.apache.org 取），解压到 `/opt/flink-<版本>`，再给一个稳定的 `/opt/flink` 软链；官方包的 `conf/flink-conf.yaml` 全是注释，现代 JDK 下集群没有显式的 rpc / 内存键起不来，所以只在文件**全是注释**时补一组最小配置。nexmark 是栈的吞吐基准口径：`nexmark-flink` 没有发布件，要从 `github.com/nexmark/nexmark` 源码 `mvn` 构建，jar 放进 Flink `lib/`，查询与脚本（`nexmark-flink/src/main/resources`）铺成 `/opt/nexmark` 可运行树。本 skill 把这两件事做成**可先看计划再执行**的流程，是 `flink-velox-install` 安装部署家族的第二个 skill，是 env 检查家族里 flink-dist-check / nexmark-check 两项 fail 的修复动作。安装表与判据细节在 [install-table.md](references/install-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 新机器 / 新容器上第一次装 Flink 1.19 运行时
- `flink-velox-flink-dist-check` 报找不到 Flink / 安装不全 / 版本不在 1.19 线
- 要跑 nexmark 基准（性能基准或正确性校验）之前，装 nexmark jar 与可运行树（`flink-velox-nexmark-check` 报缺）

### 不适用（不要用本 skill）

- 只想知道装没装、装在哪、什么版本 → `flink-velox-flink-dist-check` / `flink-velox-nexmark-check`（只读）
- 编译工具链、构建依赖、源码仓、源码级 C++ 库 → `flink-velox-build-env-prepare`
- 编译 velox4j / gluten-flink 并部署进 lib → `flink-velox-stack-build`（规划中）
- 装 1.19 线以外的 Flink：`plan` 与 `apply` 都直接拒绝（栈只在 1.19.x 上构建与验证）

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 逐项探测，列出每个步骤的状态（`TODO` / `SKIP` / `BLOCKED`）与**将执行的精确命令**（下载 URL、校验、解压、软链、conf、clone、mvn、cp） | 否 |
| `apply --yes` | 执行 TODO 的步骤：已满足的跳过；第一步失败即停，报告已做 / 未做；变更清单落盘 | 是（下载、写 /opt、克隆、构建） |
| `verify` | 运行 `flink-velox-env` 家族的 flink-dist-check 与 nexmark-check 并汇总 | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）。agent 先把 `plan` 的输出给用户看、得到明确同意后再加 `--yes`。`/opt` 对当前用户不可写时 `apply` 要求 root（提示用 `sudo` 运行），不做交互式 sudo。

## 三个步骤

| 步骤 | 内容 | 幂等判据 | 需 root |
|---|---|---|---|
| `flink` | `<prefix>/flink-<版本>/bin/flink` 不存在 → 从 `--mirror`（默认华为云）下载 `flink-<版本>-bin-scala_2.12.tgz`，失败再从 `archive.apache.org`；sha512 **恒**从 archive.apache.org 取并 `sha512sum -c`；`tar -xzf` 进 `<prefix>`。软链 `<prefix>/flink` → 版本目录不存在或指错 → `ln -sfn`（目标位置是真实目录而非软链时**不覆盖**，判失败）。`conf/flink-conf.yaml` 全是注释 → 追加 11 个最小键（rpc / bind-host / rest / 内存 / slots） | 发行版在、软链指对、conf 有有效行 | `<prefix>` 不可写时 |
| `nexmark-jar` | Flink `lib/*nexmark*.jar` 存在 → 跳过；否则：源码 `<repos-dir>/nexmark/.git` 不存在 → `git clone --depth 1 https://github.com/nexmark/nexmark.git`（失败重试 3 次并清半截目录）→ `JAVA_HOME=<JDK 17 或 8> mvn -pl nexmark-flink -am package -DskipTests`（在源码目录）→ 取 `nexmark-flink/target/nexmark-flink-*.jar`（排除 `-sources`）拷进 Flink `lib/`。前置：`mvn` 在 PATH、找得到 JDK（`JAVA_HOME` → `/usr/lib/jvm/java-17-*` → `java-1.8.0-openjdk-*`），缺一则 `BLOCKED` | jar 已在 lib | Flink lib 不可写时 |
| `nexmark-tree` | `<nexmark-home>/bin/run_query.sh` 可执行 → 跳过；否则把源码 `nexmark-flink/src/main/resources/{bin,conf,queries,queries-cep}` 拷到 `<nexmark-home>/`，建 `lib/ log/ data/`，jar 再拷一份进 `<nexmark-home>/lib/`。jar 已部署但没有源码 → `BLOCKED`（给 clone 命令） | run_query.sh 在 | `<nexmark-home>` 不可写时 |

`--no-nexmark` 只装 Flink。`--version` 只接受 `1.19.x`；`--prefix`（默认 `/opt`）、`--nexmark-home`（默认 `/opt/nexmark`）、`--repos-dir`（默认 `~/repos`）、`--mirror`（默认 `https://mirrors.huaweicloud.com/apache/flink`）可改。装完提示 `export FLINK_HOME=<prefix>/flink`、`export NEXMARK_HOME=<nexmark-home>`——本 skill 不改 shell 配置。

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/runtime_install.py plan [--version X] [--prefix DIR] [--nexmark-home DIR] [--repos-dir DIR] [--no-nexmark] [--json] [--out FILE [--overwrite]]
```

人读报告每步一行：状态、缺什么、将执行的命令（需 root 的步骤标 `# needs root`），末尾 `PLAN: N step(s) to apply`（有 BLOCKED 时注明）与 `next:`。要机器消费加 `--json`，要留档加 `--out <项目内路径>/install-plan.json`（规则见「环境约定」）。

### 步骤 2：征得同意后执行

```bash
sudo python3 <skill_dir>/scripts/runtime_install.py apply --yes [--version X] [--prefix DIR] ...
```

退出码：**0** 全部完成或已满足；**1** 某步失败即停——`changes:` 列出已做的每件事，`next:` 说明修哪一步，修完重跑（幂等）；**2** 拒绝——缺 `--yes`、需 root 而非 root、版本不在 1.19 线、某步 `BLOCKED`（缺 mvn / JDK / nexmark 源码）。

### 步骤 3：复查

```bash
python3 <skill_dir>/scripts/runtime_install.py verify
```

调 flink-dist-check 与 nexmark-check（兄弟 skill 没装在旁边则记 `skipped`）。退出码 0 全过 / 1 有 fail。

## 环境约定

- **plan 不改任何东西**；apply 只做 TODO 的步骤；下载在临时目录完成，校验通过才解压；软链目标位置若已是真实目录则不覆盖；不自动删除任何已有目录
- conf 只在**每一行都是注释或空行**时追加最小键——只要有一行有效配置就完全不碰（用户已配置）
- `ENV_PREPARE_SYSROOT`（给默认的 /opt、/usr/lib/jvm 加前缀）、`ENV_PREPARE_HOME`（替换 ~）、`ENV_PREPARE_EUID`、`ENV_PREPARE_PREFIX_WRITABLE=0`（强制视为不可写）、`ENV_PREPARE_ENV_FAMILY_DIR`、`ENV_PREPARE_RETRY_SLEEP` 仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`command`、`version`、`flink_home`、`steps[].status`、`changes`、`root_steps`、`next_step`、`error_message` 必须保留（下例 plan，节选）：

```json
{
  "flink_velox_runtime_install_result": {
    "success": true,
    "checked_at": "2026-11-24T15:00:00+08:00",
    "command": "plan",
    "version": "1.19.2", "prefix": "/opt", "flink_home": "/opt/flink", "nexmark_home": "/opt/nexmark",
    "steps": [
      {"name": "flink", "status": "todo", "needs_root": true, "dist": "/opt/flink-1.19.2", "link": "/opt/flink",
       "pending": ["download+unpack", "symlink", "minimal-conf"],
       "detail": "Flink 1.19.2: download+unpack, symlink, minimal-conf into /opt",
       "plan": ["# flink-1.19.2-bin-scala_2.12.tgz: try https://mirrors.huaweicloud.com/..., then https://archive.apache.org/dist/flink/...", "curl -fsSL --retry 3 --max-time 3600 -o <tmp>/flink-1.19.2-bin-scala_2.12.tgz <url>", "…"], "actions": []},
      {"name": "nexmark-jar", "status": "todo", "needs_root": true, "src": "/root/repos/nexmark", "jdk": "/usr/lib/jvm/java-17-openjdk",
       "detail": "build nexmark-flink from /root/repos/nexmark with /usr/lib/jvm/java-17-openjdk and copy the jar into /opt/flink/lib",
       "plan": ["git clone --depth 1 https://github.com/nexmark/nexmark.git /root/repos/nexmark   (retries 3)", "# JAVA_HOME=… mvn -pl nexmark-flink -am package -DskipTests   (in …)", "# cp …/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> /opt/flink/lib/"], "actions": []},
      {"name": "nexmark-tree", "status": "todo", "needs_root": true, "home": "/opt/nexmark", "res": "/root/repos/nexmark/nexmark-flink/src/main/resources",
       "detail": "lay out the runnable tree at /opt/nexmark from …/src/main/resources", "plan": ["mkdir -p /opt/nexmark", "cp -R …/{bin,conf,queries,queries-cep} /opt/nexmark/", "mkdir -p /opt/nexmark/{lib,log,data}", "cp /opt/flink/lib/nexmark-flink-*.jar /opt/nexmark/lib/"], "actions": []}
    ],
    "changes": [],
    "root_steps": ["flink", "nexmark-jar", "nexmark-tree"],
    "next_step": "as root: review the plan above, then run runtime_install.py apply --yes",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`；`flink` 有 `dist` / `link` / `tgz` / `pending`；`nexmark-jar` 有 `src` / `jdk`；`nexmark-tree` 有 `home` / `res`
- `changes`（apply）：`[{step, action: downloaded+unpacked|symlinked|configured|cloned|built+deployed|laid out|skipped|failed, detail}]`
- `next_step`：plan 给"看计划后 apply"（需 root 时注明 as root），apply 失败给修哪一步，成功给 verify；`error_message`：`refused: ...` / `<step> failed` / `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（flink-dist / nexmark 各一条）

## 边界与相邻 skill

- `flink-velox-env` 家族：flink-dist-check / nexmark-check——本 skill 的 `verify` 调它们；它们的 `fix` 就是本 skill 的三个步骤
- `flink-velox-build-env-prepare`：构建依赖与源码仓（maven、JDK、git 由它装；nexmark 构建用到）——与本 skill 互不依赖，但装 nexmark 前要先跑它
- `flink-velox-stack-build`（规划中）：用本 skill 装好的 Flink 与前者的构建环境编译 velox4j / gluten-flink 并部署进 lib
- nexmark 吞吐基准 / 正确性校验 → 基准 / 校验类 skill；本 skill 只把 jar 与可运行树放好
