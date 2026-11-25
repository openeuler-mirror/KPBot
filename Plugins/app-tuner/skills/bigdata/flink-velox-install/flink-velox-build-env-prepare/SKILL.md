---
name: flink-velox-build-env-prepare
description: >
  Flink + GlutenFlink（velox 向量化栈）构建环境准备——克隆 velox / velox4j / gluten 三个源码仓、装 openEuler 系统包
  （工具链 + 构建工具 + 13 个 C++ 库 + JDK 17）、按 velox 自带 setup 脚本装源码级 C++ 库进 /usr/local。三段式：
  `plan`（默认）只打印将执行的命令与变更、不动机器；`apply --yes` 才执行（幂等、失败即停、记录变更清单；需 root 的步骤
  非 root 下拒绝）；`verify` 调 flink-velox-env 家族的五个检查复查。触发词：准备构建环境、装依赖、克隆源码仓、装 C++ 库、
  setup 脚本、source-deps、build env。不负责：只查不装 → flink-velox-env 家族各检查；装 Flink 运行时 →
  flink-velox-runtime-install；编译部署 GlutenFlink 构建 → flink-velox-stack-build。
compatibility: Linux openEuler / CentOS 9 / RHEL 9（apply；包名按 openEuler），Python 3.7+ 标准库；apply 需 root（dnf 与写 /usr/local）；经 sudo 运行时克隆以原用户执行（需 runuser 在 PATH）
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "false"
  version: "1.0.0"
---

# Flink-velox 构建环境准备

要在本机编译 velox4j 与 gluten-flink，得有三样东西：三个源码仓（velox / velox4j / gluten，基线分支见下表）、一批系统包（工具链、构建工具、13 个 C++ 库、JDK 17），以及一批**不在包管理器里**的 C++ 库——velox 自己的 setup 脚本从源码构建 boost / folly 链 / protobuf / arrow 等装进 `/usr/local`。本 skill 把这三步做成**可先看计划再执行**的流程：`plan` 列出将要运行的每条命令，`apply --yes` 执行（已装 / 已克隆 / 已构建的跳过，某步失败即停并报告做到哪了），`verify` 调 env 检查家族复查。它是 `flink-velox-install` 安装部署家族的成员，是 env 检查家族里 toolchain / build-tools / cpp-deps / jdk / source-repos 五项 fail 的修复动作。探测与判据细节在 [prepare-table.md](references/prepare-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 新机器 / 新容器上第一次搭 GlutenFlink 构建环境
- `flink-velox-env` 检查家族报 toolchain / build-tools / cpp-deps / source-repos / jdk 缺项，要照单补齐
- velox checkout 换了分支后，setup 脚本里的源码级库清单变了，要把缺的补上

### 不适用（不要用本 skill）

- 只想知道缺什么、不要动机器 → `flink-velox-env` 检查家族各具体项检查（只读，见 !268 / !320）
- 安装 Flink 1.19 运行时与 nexmark → `flink-velox-runtime-install`（见 !323）
- 编译 velox4j / gluten-flink 并部署进 Flink lib → `flink-velox-stack-build`（见 !324）
- 非 openEuler / CentOS 9 / RHEL 9 的机器：`plan` 照常列出，但 `apply` 会拒绝（包名是 openEuler 的，没在别的发行版上验证）

## 三段式

| 子命令 | 做什么 | 改机器 |
|---|---|---|
| `plan`（默认） | 逐项探测，列出每个步骤的状态（`TODO` / `SKIP` / `BLOCKED`）与**将执行的精确命令** | 否 |
| `apply --yes` | 执行 TODO 的步骤：已满足的跳过；第一步失败即停，`changes` 逐条列出已做的事 | 是 |
| `verify` | 依次运行 env 检查家族的 toolchain / build-tools / cpp-deps / jdk / source-repos 五个检查并汇总；兄弟检查 skill 没装时报 `NOT VERIFIED` | 否 |

**同意机制**：`apply` 没有 `--yes` 直接拒绝（退出 2）。agent 的纪律是先把 `plan` 的输出给用户看、得到明确同意后再加 `--yes`——`--yes` 就是那次同意的记录。经 `sudo` 运行时**只有装包与往 /usr/local 装库以 root 执行**，克隆源码仓交回原用户（`SUDO_USER`，经 `runuser`），`~/repos` 也是原用户的。plan 用 `--out` 留档后，apply 带 `--plan <该文件>` 把同意落到指纹上：对 root 将要 source 的 setup 脚本重新校验 sha256 与 velox checkout 的 origin / HEAD（apply 开始时与 source-deps 执行前各一次），与 plan 记录不一致、或记录指纹的 checkout 没了（apply 只能重新克隆）一律拒绝（退出 2）——`--yes` 同意的是 plan 时的指纹，指纹变了就不能继续。

## 三个步骤

| 步骤 | 内容 | 幂等判据 | 需 root |
|---|---|---|---|
| `packages` | `dnf install -y <缺的包>`：工具链 git cmake gcc gcc-c++ maven、构建工具 ninja-build ccache autoconf automake libtool flex bison python3 patchelf、13 个 C++ `-devel` 库、`java-17-openjdk-devel`（共 28 个，openEuler 包名） | `rpm -q <包>` 已装 | 是 |
| `repos` | `git clone -b <基线> <上游> <repos-dir>/<仓>`：velox（bigo-sg，`gluten-20260829`）、velox4j（bigo-sg，`gluten-20260829`）、gluten（apache，`main`）。**每次尝试在 `<repos-dir>/.<仓>-clone-*/` 这个本次独占的临时目录里克隆，完整后才挪到目标位置**；失败重试 3 次，只清理本次的临时目录——目标位置上已有的东西（别人刚建的同名目录、半截下载）一律不碰 | 目录存在且标志文件齐（velox：`CMakeLists.txt` + `scripts/`；velox4j：`pom.xml`；gluten：`gluten-flink/pom.xml`）；目录在但标志文件缺 → `BLOCKED`，**不自动删**；`git` 不在 PATH 但 packages 步骤会装 → 不阻塞（计划里注明） | 否 |
| `source-deps` | 从 velox checkout 的 `scripts/setup-centos9.sh`（Ubuntu 为 `setup-ubuntu.sh`）解析 `install_velox_deps` 里的 `install_<lib>` 序列（去包管理器与 conda 条目），对 `/usr/local` 下探不到的库，在子 shell 里 `source` 脚本后调 `install_<lib>()`，`INSTALL_PREFIX=/usr/local`、`BUILD_THREADS=min(32, --jobs 或核数)`、`PROMPT_ALWAYS_RESPOND=n`；按脚本顺序逐库构建，第一个失败即停（后面的库依赖前面的）。**脚本是 root 执行的：它来自哪个 checkout（origin / commit / 是否有本地改动）与脚本自身的 sha256 记在计划与 `changes` 里** | cmake 配置目录 / include 目录 / 头文件 / 探针二进制（同 cpp-deps-check 的探测）任一在 /usr/local 下存在 | 是 |

`repos-dir`：`--repos-dir` → `$GFV_REPOS_DIR` → `~/repos`。velox 还没克隆时 `plan` 里 `source-deps` 显示为 TODO（清单要从克隆下来的脚本解析），`apply` 在 `repos` 完成后重新探测。`next:` 给出的 apply / verify 命令是完整命令——`python3`、本脚本的绝对路径、本次显式传入的 `--repos-dir` / `--jobs`——在任意目录照抄即可。

## 执行步骤

### 步骤 1：看计划

```bash
python3 <skill_dir>/scripts/build_env_prepare.py plan [--repos-dir DIR] [--json] [--out FILE [--overwrite]]
```

人读报告每步一行：状态、缺什么、将执行的命令（`# needs root` 标注需 root 的步骤），末尾 `PLAN: N step(s) to apply` 与 `next:`。要机器消费加 `--json`，要留档加 `--out <项目内路径>/prepare-plan.json`（规则见「环境约定」）。

### 步骤 2：征得同意后执行

```bash
sudo python3 <skill_dir>/scripts/build_env_prepare.py apply --yes [--repos-dir DIR] [--jobs N] [--plan FILE] [--json] [--out FILE]
```

退出码：**0** 全部完成或已满足；**1** 某步失败即停——`changes:` 逐条列出已做的每件事（命令无法执行或超时也算该步失败，记录不清空），`next:` 说明修哪一步，修完重跑 `plan` / `apply`（幂等，不会重复装 / 克隆 / 构建）；**2** 拒绝——缺 `--yes`、需 root 的步骤在非 root 下运行（提示里给出带 `sudo` 的同一条命令）、发行版不受支持、某步 `BLOCKED`（不完整的 checkout、`dnf` 不在 PATH、没有 velox checkout 可解析）、经 sudo 而 `runuser` 不在 PATH、`--out` 不合法（此时什么都没做）、`--plan` 记录读不了或校验不过（setup 脚本的 sha256 / velox origin / HEAD 与 plan 记录不一致，或记录指纹的 checkout 没了——脚本在 plan 与 apply 之间被换或 checkout 被改，重跑 plan 请用户重新过目；校验在 apply 开始与 source-deps 执行前各做一次，前者机器未动，后者 `changes` 保留已完成步骤）。

### 步骤 3：复查

```bash
python3 <skill_dir>/scripts/build_env_prepare.py verify [--repos-dir DIR]
```

调 env 家族五个检查——其中 toolchain / build-tools / cpp-deps 已随 !268 在仓，jdk 与 source-repos 两项的 skill 随 !320 合入：**!320 合入前这两项检查在本仓还不存在**，verify 对它们报 `missing`（detail 注明 "delivered by !320 (pending merge)"）、整体 `NOT VERIFIED`。这是既定纪律：兄弟检查缺失不算验证通过——装上这两个 skill 或等 !320 合入后，verify 才是五个维度的完整复查。退出码 **0** 全部通过；**1** 有检查失败，或兄弟检查 skill 没装（一项都没查，报 `NOT VERIFIED` 并列出缺哪些——不是通过）。兄弟检查按 skill 名定位，不依赖目录层级：`ENV_PREPARE_ENV_FAMILY_DIR` 指定了目录就只认它（没装就是 `missing`）；否则依次找源码树的 `skills/bigdata/flink-velox-env/<检查 skill>/`、安装后平铺的 `<config>/skills/<检查 skill>/`。装完后把 `JAVA_HOME` 指到 JDK 17（`/usr/lib/jvm/java-17-*`）——本 skill 不改 shell 配置。

## 环境约定

- **plan 不改任何东西**；apply 只做 TODO 的步骤；**不自动删除任何目录或文件**（不完整 checkout、下载中断留下的依赖目录、用户已有的东西一律保留并报告）；不自动卸载包、不自动回滚——回退靠变更清单（`changes`）人工处理
- 依赖目录已存在但没有构建入口文件（CMakeLists / configure / Makefile / bootstrap.sh / setup.py）时，velox 会把它当作已下载而永远跳过——本 skill **不清它**：该库的步骤判失败并指出目录位置，由用户决定删还是修
- 子 shell 驱动 velox 官方 install 函数：`source` 脚本后调 `install_<lib>()`，脚本的 `set -e` / 导出变量被限制在子 shell 内。这一步必须是 root（写 /usr/local），所以做不到降权，改为留档：velox checkout 的 origin / commit / 本地改动、脚本 sha256 都进计划与 `changes`
- 经 `sudo` 运行时克隆以 `SUDO_USER` 身份执行（`runuser -u <user> --`，保留环境变量、HOME 换成该用户的），只有装包与装库以 root 执行。root 建的目录归 root 且是 0700，降权用户进不去：本次新建的 `<repos-dir>` 与克隆用的临时目录建出来就交给该用户（`chown`），对 checkout 的 git 查询也以该用户执行
- `ENV_PREPARE_SYSROOT`（绝对系统路径前缀）、`ENV_PREPARE_HOME`（替换 ~）、`ENV_PREPARE_EUID`（覆盖有效 uid）、`ENV_PREPARE_ENV_FAMILY_DIR`（verify 找 env 家族的目录）、`ENV_PREPARE_RETRY_SLEEP`（克隆重试间隔秒）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限，且**在任何动作开始前校验**：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；已存在的目标不是普通文件（管道、目录等）时即使带 `--overwrite` 也拒绝。被拒绝时退出码 2、stderr 说明原因，apply 不会开始

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`command`、`os`、`steps[].status`、`changes`、`root_steps`、`next_step`、`error_message` 必须保留（下例 plan，节选）：

```json
{
  "flink_velox_build_env_prepare_result": {
    "success": true,
    "checked_at": "2026-11-24T14:00:00+08:00",
    "command": "plan",
    "os": {"id": "openEuler", "version": "24.03", "supported": true, "label": "openEuler"},
    "repos_dir": "/home/me/repos",
    "steps": [
      {"name": "packages", "status": "todo", "needs_root": true, "missing": ["git", "cmake"], "present": 26,
       "detail": "2 of 28 packages missing: git, cmake", "plan": ["dnf install -y git cmake"], "actions": []},
      {"name": "repos", "status": "todo", "needs_root": false,
       "detail": "3 checkout(s) to clone into /home/me/repos: velox, velox4j, gluten; git is not on PATH yet — the packages step installs it first",
       "plan": ["git clone -b gluten-20260829 https://github.com/bigo-sg/velox.git /home/me/repos/velox", "..."],
       "clones": [{"name": "velox", "url": "https://github.com/bigo-sg/velox.git", "branch": "gluten-20260829", "dest": "/home/me/repos/velox"}], "actions": []},
      {"name": "source-deps", "status": "todo", "deferred": true, "needs_root": true,
       "detail": "library list is parsed from /home/me/repos/velox/scripts/setup-centos9.sh once the repos step has cloned velox",
       "plan": ["# after the repos step: source ... && install_<lib>() for every library missing under /usr/local, in setup-script order"], "actions": []}
    ],
    "changes": [],
    "root_steps": ["packages", "source-deps"],
    "next_step": "as root: review the plan above, then run python3 <skill_dir>/scripts/build_env_prepare.py apply --yes --repos-dir /home/me/repos",
    "error_message": ""
  }
}
```

- `steps[].status`：`todo` / `skipped` / `done` / `failed` / `blocked`；`source-deps` 在 velox 未克隆时 `deferred: true`，已克隆时另有 `setup` / `libs` / `missing` / `source`（velox checkout 的 `origin` / `head` / `dirty`，不是 git checkout 时三项为 `null`）/ `setup_sha256`；`packages` 有 `missing` / `present`；`repos` 有 `clones`
- `changes`（apply）：`[{step, action, detail}]`，`action` 取 `installed` / `cloned` / `clone failed` / `source recorded`（脚本路径、sha256、来自哪个 checkout）/ `built` / `skipped` / `failed`——做了什么的完整清单，失败时也带
- `next_step`：plan 给"看计划后 apply"（需 root 时注明 as root；给出的命令都是完整的——`python3` + 本脚本绝对路径——并带着本次的显式选项），apply 失败给修哪一步，apply 成功给 verify；`error_message`：apply 被拒绝时 `refused: ...`（含 `--plan` 指纹校验拒绝：`the setup script changed between plan and apply (setup script sha256 … -> …; velox … @ … -> …) — rerun plan and review, then apply again`），失败时 `<step> failed`，工具崩溃时 `tool aborted:`（退出 2，JSON 仍是完整契约形态）
- `verify` 另有 `checks[]`（五个检查各一条）：`status` 取 `ok` / `fail` / `missing`（兄弟检查 skill 没装，带 `skill` 名）；随未合入 PR 交付的检查（jdk / source-repos ← !320）在 `missing` 项里带 `pending_pr` 字段、detail 写 "…delivered by !320 (pending merge)"；有 `missing` 或 `fail` 时 `success=false`、退出 1，`error_message` 写"N check(s) not run (sibling skills not installed): …"或"N check(s) failing"；`next_step` 对 `missing` 提示装 skill 或等 !320 合入、对 `fail` 提示修失败项，两类并存时依次都给

## 边界与相邻 skill

- env 检查家族的 toolchain / build-tools / cpp-deps / jdk / source-repos 五个检查（见 !268 / !320）——本 skill 的 `verify` 调它们；它们的 `fix` 就是本 skill 的三个步骤
- `flink-velox-runtime-install`（见 !323）：Flink 1.19 运行时与 nexmark——与本 skill 互不依赖
- `flink-velox-stack-build`（见 !324）：用本 skill 准备好的环境编译 velox4j / gluten-flink 并部署进 lib
- 本 skill 不编译任何 GlutenFlink 组件、不装 Flink、不碰集群
