---
name: flink-velox-runtime-install
description: >
  安装 Flink + GlutenFlink（velox 向量化栈）所依赖的 Flink 运行时与 nexmark 基准件——官方 Flink 1.19.x 二进制包
  （镜像下载，sha512 恒取 archive.apache.org 校验）解压到 /opt/flink-<版本> + /opt/flink 软链 + Flink 实际读的那份 conf 全是注释时补最小配置；
  把 nexmark 源码取到固定 commit、mvn 构建 nexmark-flink jar 放进该版本的 Flink lib、按清单铺 /opt/nexmark 可运行树。
  三段式：`plan`（默认）只打印、`apply --yes` 才执行（幂等、失败即停、变更清单；/opt 不可写时非 root 拒绝）；`verify` 调 flink-dist-check 与 nexmark-check。
  触发词：安装 Flink、装 Flink 运行时、下载 flink tgz、/opt/flink、装 nexmark、nexmark jar、run_query.sh、NEXMARK_HOME。
  不负责：查装没装 → flink-velox-flink-dist-check / nexmark-check；装构建依赖 → flink-velox-build-env-prepare；
  编译部署 GlutenFlink 构建 → flink-velox-stack-build。
compatibility: Linux（openEuler 验证），Python 3.7+ 标准库；需 curl、tar、sha512sum；nexmark 需 git、mvn、JDK 8|17；只接受 1.19 版本线；经 sudo 运行时 git 以原用户执行（需 runuser 在 PATH），装进系统目录的产物由 root 从逐文件核对过的副本构建
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# Flink 运行时与 nexmark 安装

GlutenFlink 栈跑在官方 Flink 发行版上：velox4j 与 gluten-flink 的产物部署进它的 `lib/`，集群从它的 `bin/` 起停。发行版是 Apache 的 tgz（国内从华为云镜像取，校验文件只从 archive.apache.org 取），解压到 `/opt/flink-<版本>`，再给一个稳定的 `/opt/flink` 软链；现代 JDK 下集群没有显式的 rpc / 内存键起不来，所以只在 Flink 实际读的那份配置**全是注释**时补一组最小配置（1.19 两份并存时只读 `flink-conf.yaml`，否则读 `config.yaml`）。nexmark 是栈的吞吐基准口径：`nexmark-flink` 没有发布件，要从 `github.com/nexmark/nexmark` 源码的一个**固定 commit** `mvn` 构建，jar 放进**正在安装的版本**的 `lib/`，查询与脚本（`nexmark-flink/src/main/resources`）按清单铺成 `/opt/nexmark` 可运行树。本 skill 把这两件事做成**可先看计划再执行**的流程，是 `flink-velox-install` 安装部署家族的成员，是 env 检查家族里 flink-dist-check / nexmark-check 两项 fail 的修复动作。安装表与判据细节在 [install-table.md](references/install-table.md)。

## 术语

这套栈的官方叫法是 GlutenFlink；`GFV` 是代码与契约层沿用的缩写，指同一构建，按既有契约保留原名——环境变量 `GFV_REPOS_DIR`（`--repos-dir` 没给时的取值来源）、项目名 GFVBot（本 skill 的安装表判据解析自其安装器的 `install_flink` / `install_nexmark`）。

## 使用场景

### 适用（应使用本 skill）

- 新机器 / 新容器上第一次装 Flink 1.19 运行时
- `flink-velox-flink-dist-check` 报找不到 Flink / 安装不全 / 版本不在 1.19 线
- 要跑 nexmark 基准（性能基准或正确性校验）之前，装 nexmark jar 与可运行树（`flink-velox-nexmark-check` 报缺）

### 不适用（不要用本 skill）

- 只想知道装没装、装在哪、什么版本 → `flink-velox-flink-dist-check` / `flink-velox-nexmark-check`（只读）
- 编译工具链、构建依赖、源码仓、源码级 C++ 库 → `flink-velox-build-env-prepare`（见 !322）
- 编译 velox4j / gluten-flink 并部署进 lib → `flink-velox-stack-build`（见 !324）
- 装 1.19 线以外的 Flink：`plan` 与 `apply` 都直接拒绝（栈只在 1.19.x 上构建与验证）

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 逐项探测，列出每个步骤的状态（`TODO` / `SKIP` / `BLOCKED`）与**将执行的精确命令**（下载 URL、校验、解压、软链、conf、取源码、mvn、cp） | 否 |
| `apply --yes` | 执行 TODO 的步骤：已满足的跳过；第一步失败即停，`changes` 逐条列出已做的每件事 | 是（下载、写 /opt、取源码、构建） |
| `verify` | 运行 env 检查家族的 flink-dist-check 与 nexmark-check 并汇总；兄弟检查 skill 没装时报 `NOT VERIFIED` | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）。agent 先把 `plan` 的输出给用户看、得到明确同意后再加 `--yes`。`/opt` 对当前用户不可写时 `apply` 要求 root（提示用 `sudo` 运行），不做交互式 sudo；**谁安装决定谁构建**：装进 root 拥有的目录（euid 为 0）时只构建官方 commit——`--nexmark-ref` 须是 commit 形态（tag / 分支直接拒绝），checkout 须来自官方 origin、在要求的 commit 上、没有本地改动，构建在一份 root 拥有的副本里进行，副本的每个文件都与 commit 的 tree 比对（`git ls-tree`，基准是钉死 SHA、`refs/replace` 关闭），checkout 的主人影响不了 jar 与可运行树；经 `sudo` 运行时 git 以原用户执行（`SUDO_USER`，经 `runuser`；checkout 在他自己的 home 里），Maven 构建是 root 的，用 root 自己的 `~/.m2`。非 root、前缀自己可写时，用户的 checkout 原样构建（tag / 分支也行）。

## 三个步骤

| 步骤 | 内容 | 幂等判据 | 需 root |
|---|---|---|---|
| `flink` | `<prefix>/flink-<版本>/bin/flink` 不存在 → 从 `--mirror`（默认华为云）下载 `flink-<版本>-bin-scala_2.12.tgz`，失败再从 `archive.apache.org`；sha512 **恒**从 archive.apache.org 取并 `sha512sum -c`；`tar -xzf` 进 `<prefix>`。软链 `<prefix>/flink` → 版本目录不存在或指错 → 经本次运行独占的临时名 `ln -sfn`（目标位置是真实目录而非软链时**不覆盖**，判失败）。Flink 实际读的那份 conf 全是注释 → 追加 11 个最小键（rpc / bind-host / rest / 内存 / slots） | 发行版在、软链指对、conf 有有效行 | `<prefix>` 不可写时 |
| `nexmark-jar` | **正在安装的版本目录**的 `lib/*nexmark*.jar` 存在 → 跳过（**不再静默**：detail 逐个列出现有 jar 的 sha256 与本次请求的 `--nexmark-ref`，并说明要换 ref 须先移除旧 jar 或装别的版本 / prefix）；否则：源码 `<repos-dir>/nexmark` 不存在 → 取到固定 commit（`--nexmark-ref`，默认见安装表）并核对 `HEAD`，完整后挪到位 → `JAVA_HOME=<JDK 17 或 8> mvn -pl nexmark-flink -am package -DskipTests`（非 root 在源码目录；root 在一份逐文件核对过的副本里，见「环境约定」）→ 取 `nexmark-flink/target/nexmark-flink-*.jar`（排除 `-sources`）拷进该版本的 `lib/`，jar 的 sha256 记入 `changes`。`BLOCKED`：缺 mvn / git / JDK / runuser；目标位置已有东西但不是 git checkout（**不克隆覆盖、不删除**）；已有 checkout 在别的 commit；由 root 安装而 checkout 的 origin 不对或有本地改动 | jar 已在该版本的 lib | 该 lib 不可写时 |
| `nexmark-tree` | `<nexmark-home>` 按清单齐全（`bin/run_query.sh`、`bin/config.sh`、`conf/{log4j.properties,nexmark.yaml,config.yaml}`、`queries/{ddl_*,insert_kafka}.sql`、至少一个 `queries/q*.sql`）→ 跳过；否则从源码 `nexmark-flink/src/main/resources/{bin,conf,queries,queries-cep}` 补齐缺的文件（**已有的不覆盖**；由 root 安装时取自逐文件核对过的副本，jar 已部署时也先核对再拷），建 `lib/ log/ data/`，jar 再拷一份进 `<nexmark-home>/lib/`。jar 已部署而没有源码可补 → `BLOCKED`（给取源码的命令） | 清单齐全 | `<nexmark-home>` 不可写时 |

`--no-nexmark` 只装 Flink。`--version` 只接受 `1.19.x`；`--prefix`（默认 `/opt`）、`--nexmark-home`（默认 `/opt/nexmark`）、`--repos-dir`（默认 `~/repos`）、`--mirror`（默认 `https://mirrors.huaweicloud.com/apache/flink`）、`--nexmark-ref`（默认钉到安装表里的 commit）可改。装完提示 `export FLINK_HOME=<prefix>/flink`、`export NEXMARK_HOME=<nexmark-home>`——本 skill 不改 shell 配置。

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/runtime_install.py plan [--version X] [--prefix DIR] [--nexmark-home DIR] [--repos-dir DIR] [--mirror URL] [--nexmark-ref REF] [--no-nexmark] [--json] [--out FILE [--overwrite]]
```

人读报告每步一行：状态、缺什么、将执行的命令（需 root 的步骤标 `# needs root`），末尾 `PLAN: N step(s) to apply`（有 BLOCKED 时注明）与 `next:`。`next:` 给出的 apply / verify 命令是完整命令——`python3`、本脚本的绝对路径、本次显式传入的选项——在任意目录照抄即可。要机器消费加 `--json`，要留档加 `--out <项目内路径>/install-plan.json`（规则见「环境约定」）。

### 步骤 2：征得同意后执行

```bash
sudo python3 <skill_dir>/scripts/runtime_install.py apply --yes [--version X] [--prefix DIR] ...
```

退出码：**0** 全部完成或已满足；**1** 某步失败即停——`changes:` 逐条列出已做的每件事（命令无法执行或超时也算该步失败，记录不清空），`next:` 说明修哪一步，修完重跑（幂等）；**2** 拒绝——缺 `--yes`、需 root 而非 root（提示里给出带 `sudo` 的同一条命令）、版本不在 1.19 线、由 root 安装而 `--nexmark-ref` 不是 commit 形态、某步 `BLOCKED`、`--out` 不合法（此时什么都没做）。

### 步骤 3：复查

```bash
python3 <skill_dir>/scripts/runtime_install.py verify [--prefix DIR] [--nexmark-home DIR] [--no-nexmark]
```

调 flink-dist-check 与 nexmark-check。退出码 **0** 全部通过；**1** 有检查失败，或兄弟检查 skill 没装（一项都没查，报 `NOT VERIFIED` 并列出缺哪个——不是通过）；`--no-nexmark` 时 nexmark-check 记 `disabled`，不算缺。兄弟检查按 skill 名定位，不依赖目录层级：`ENV_PREPARE_ENV_FAMILY_DIR` 指定了目录就只认它（没装就是 `missing`）；否则依次找源码树的 `skills/bigdata/flink-velox-env/<检查 skill>/`、安装后平铺的 `<config>/skills/<检查 skill>/`。

## 环境约定

- **plan 不改任何东西**；apply 只做 TODO 的步骤；下载在本次运行的临时目录完成，校验通过才解压；软链目标位置若已是真实目录则不覆盖；**不删除、不覆盖任何已有的目录或文件**——目标位置已有非 checkout 的目录、checkout 在别的 commit，都只报告
- conf 只在 Flink 实际读的那一份**每一行都是注释或空行**时追加最小键——只要有一行有效配置就完全不碰（用户已配置）；1.19 两份并存时 Flink 只读 `flink-conf.yaml`
- nexmark 固定到 commit：新 checkout 取到 `--nexmark-ref`（默认是安装表里钉死的那个）并核对 `HEAD`；上游默认分支前进了也不会悄悄换版本——要用新提交须显式传 `--nexmark-ref <commit>`。**privileged 安装（由 root 装）只接受 commit 形态的 `--nexmark-ref`**：tag / 分支是可变引用、钉不死，`plan` / `apply` 直接拒绝（退出 2）；非 privileged（用户装进自己可写的前缀）tag / 分支照旧允许——装的是自己的东西。不用 `git ls-remote` 解析（机器上实证会超时）
- **信任边界跟安装特权走，不跟构建身份走**：jar 与可运行树落进 Flink 的 classpath 与 `<nexmark-home>`，由谁安装决定能信任什么。非 root（前缀是用户自己可写的目录）时装的是用户自己的东西，checkout 原样构建。由 root 安装（经 sudo 或直接是 root）时只构建官方 commit：plan 里 origin 不是官方地址、或 `git status` 报本地改动的 checkout 判 `BLOCKED`；apply 里（含本次刚克隆的 checkout）再拷一份到 `<prefix>/.nexmark-build-*/`（root 拥有、不带 `.git`），**逐文件与钉死 commit 的 tree 比对**（`git ls-tree -r -z <钉死的 commit>` 的 blob 哈希——基准取自钉死 SHA 本身而不是可动的 HEAD；多一个文件、少一个文件、改一个字节、执行位不同都算），对不上判该步 `failed` 并点名文件——不依赖 `git status` 的配置语义（`status.showUntrackedFiles`、`core.worktree` 都能让它说谎）。只读 git 查询一律带 `GIT_NO_REPLACE_OBJECTS=1`（`refs/replace` 换不掉 SHA 名下的 tree，`rev-parse` / `status` 仍显干净的伪造拦得住）；副本拷完、基准读完之后守卫（HEAD 仍等于钉死 commit、origin 仍官方、status 仍干净）再核一遍，不一致判该步 `failed`——拷贝前后守卫各核一次、基准是不可变 SHA。可运行树同样取自这份副本。副本用完即删
- 经 `sudo` 运行时 git 以 `SUDO_USER` 身份执行（`runuser -u <user> --`，保留环境变量、HOME 换成该用户的）：checkout 在原用户自己的 `~/repos` 里，root 不在它的 checkout 里跑 git（仓库配置能让 git 执行程序，例如 `core.fsmonitor`）。root 为此新建的 `<repos-dir>` 与每次克隆的临时目录建出来就 `chown` 给该用户（root 的 0700 目录降权用户进不去），`changes` 里记一条 `created directory`；Maven 构建由 root 在副本里跑，HOME 换成 root 自己的，读不到原用户的 `~/.m2/settings.xml` 与本地仓库
- `ENV_PREPARE_SYSROOT`（给默认的 /opt、/usr/lib/jvm 加前缀）、`ENV_PREPARE_HOME`（替换 ~）、`ENV_PREPARE_EUID`、`ENV_PREPARE_PREFIX_WRITABLE=0`（强制视为不可写）、`ENV_PREPARE_ENV_FAMILY_DIR`、`ENV_PREPARE_RETRY_SLEEP` 仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限，且**在任何动作开始前校验**：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；已存在的目标不是普通文件（管道、目录等）时即使带 `--overwrite` 也拒绝。被拒绝时退出码 2、stderr 说明原因，apply 不会开始

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
       "conf": "/opt/flink-1.19.2/conf/config.yaml", "pending": ["download+unpack", "symlink", "minimal-conf"],
       "detail": "Flink 1.19.2: download+unpack, symlink, minimal-conf into /opt",
       "plan": ["# flink-1.19.2-bin-scala_2.12.tgz: try https://mirrors.huaweicloud.com/..., then https://archive.apache.org/dist/flink/...", "curl -fsSL --retry 3 --max-time 3600 -o <tmp>/flink-1.19.2-bin-scala_2.12.tgz <url>", "…"], "actions": []},
      {"name": "nexmark-jar", "status": "todo", "needs_root": true, "src": "/home/me/repos/nexmark", "lib": "/opt/flink-1.19.2/lib",
       "ref": "6b3646c3baec701f1fa74baf938d235f742e5d3c", "checkout": "absent", "jdk": "/usr/lib/jvm/java-17-openjdk", "builds_from": "the checkout",
       "detail": "build nexmark-flink at 6b3646c3baec from /home/me/repos/nexmark with /usr/lib/jvm/java-17-openjdk and copy the jar into /opt/flink-1.19.2/lib",
       "plan": ["git init -q <tmp>/nexmark && git -C <tmp>/nexmark remote add origin https://github.com/nexmark/nexmark.git && git -C <tmp>/nexmark fetch --depth 1 origin 6b3646c3… && git -C <tmp>/nexmark checkout -q --detach FETCH_HEAD   (retries 3; …)", "# JAVA_HOME=… mvn -pl nexmark-flink -am package -DskipTests   (in …)", "# cp …/nexmark-flink/target/nexmark-flink-*.jar (not -sources) -> /opt/flink-1.19.2/lib/"], "actions": []},
      {"name": "nexmark-tree", "status": "todo", "needs_root": true, "home": "/opt/nexmark", "res": "/home/me/repos/nexmark/nexmark-flink/src/main/resources",
       "missing": ["bin/run_query.sh", "…"],
       "detail": "complete the runnable tree at /opt/nexmark (missing 10: …) from …/src/main/resources", "plan": ["mkdir -p /opt/nexmark", "# copy what is missing … existing files are kept", "mkdir -p /opt/nexmark/{lib,log,data}", "…"], "actions": []}
    ],
    "changes": [],
    "root_steps": ["flink", "nexmark-jar", "nexmark-tree"],
    "next_step": "as root: review the plan above, then run python3 <skill_dir>/scripts/runtime_install.py apply --yes",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`；`flink` 有 `dist` / `link` / `tgz` / `conf` / `pending`；`nexmark-jar` 有 `src` / `lib` / `ref` / `checkout`（absent / complete / conflict）/ `jdk` / `builds_from`（`the checkout`：非 root，原样构建；`a verified copy`：由 root 安装，副本逐文件核对后才构建）；`nexmark-tree` 有 `home` / `res`（todo 时）/ `missing`
- `changes`（apply）：`[{step, action, detail}]`，`action` 取 `downloaded` / `unpacked` / `symlinked` / `configured` / `created directory`（经 sudo 为原用户新建的 repos 目录）/ `cloned` / `clone failed` / `verified copy`（由 root 安装：核对过的副本，detail 带文件数与 commit）/ `built+deployed`（detail 带 jar 的 sha256）/ `laid out` / `completed` / `skipped`（nexmark-jar 的 skipped detail 带现有 jar 的 sha256 与本次请求的 ref）/ `failed`；某步失败时它之前的条目都保留
- `next_step`：plan 给"看计划后 apply"（需 root 时注明 as root；给出的命令都是完整的——`python3` + 本脚本绝对路径——并带着本次的显式选项），apply 失败给修哪一步，成功给 verify；`error_message`：`refused: ...` / `<step> failed` / `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（flink-dist / nexmark 各一条）：`status` 取 `ok` / `fail` / `missing`（兄弟检查 skill 没装，带 `skill` 名）/ `disabled`（`--no-nexmark`）；有 `missing` 或 `fail` 时 `success=false`、退出 1，`error_message` 写"N check(s) not run (sibling skills not installed): …"或"N check(s) failing"

## 边界与相邻 skill

- env 检查家族的 flink-dist-check / nexmark-check（见 !321）——本 skill 的 `verify` 调它们；它们的 `fix` 就是本 skill 的三个步骤
- `flink-velox-build-env-prepare`（见 !322）：构建依赖与源码仓（maven、JDK、git 由它装；nexmark 构建用到）——与本 skill 互不依赖，但装 nexmark 前要先跑它
- `flink-velox-stack-build`（见 !324）：用本 skill 装好的 Flink 与前者的构建环境编译 velox4j / gluten-flink 并部署进 lib
- nexmark 吞吐基准 / 正确性校验 → 基准 / 校验类 skill；本 skill 只把 jar 与可运行树放好
