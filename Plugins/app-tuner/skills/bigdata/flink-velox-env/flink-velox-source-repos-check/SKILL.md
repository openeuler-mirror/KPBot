---
name: flink-velox-source-repos-check
description: >
  Flink + GlutenFlink（velox 向量化栈）三个源码仓的只读检查：velox、velox4j、gluten 各自存在、标志文件齐全（可用的
  checkout）、经 git 读出分支与 HEAD，与构建基线分支（velox / velox4j 为 bigo-sg 的 gluten-20260829，gluten 为
  apache main）对比——偏离只提示不判负。仓库根按 --repos-dir → GFV_REPOS_DIR → 向上找 repos/ 定位，单仓可用
  VELOX_HOME / VELOX4J_HOME / GLUTEN_HOME 指定。逐项 ok/warn/fail + fix + 唯一 next_step。只扫不改：不克隆、不 fetch。
  触发词：源码仓、velox / velox4j / gluten 克隆了没、在哪个分支、基线分支、repos 目录。不负责：C++ 依赖 →
  flink-velox-cpp-deps-check；编译工具链 → flink-velox-toolchain-check；"能不能编译 GFV 栈"的总裁决 →
  flink-velox-env-check；克隆与切分支 → 安装类 skill（规划中）。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库；分支与 HEAD 经 PATH 上的 git 读取
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink-velox 源码仓检查

GFV 栈从三个源码仓构建：velox（向量化执行引擎，bigo-sg 分支 `gluten-20260829`）、velox4j（它的 Java 绑定，同分支）、gluten（`apache/gluten` 的 `main`，取其 `gluten-flink` 模块）。少一个仓、clone 得不完整（缺标志文件）、或者停在了别的分支上，编译会在半途才报错，而且"分支不对"这类问题根本不报错——只是编出来的东西没经过验证。本 skill 只做一件事：**把三个仓逐个核一遍——在不在、完不完整、在哪个分支的哪个提交上**。它是 `flink-velox-env` 检查家族里"源码仓"这一具体项：只读、不改动机器、不克隆、不 fetch。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 搭 GFV 构建环境之前或之后，确认三个源码仓都克隆了、克隆完整
- 编译报找不到 `CMakeLists.txt` / `pom.xml` / `gluten-flink/pom.xml` 时先查仓库完整性
- 要知道三个仓各自在哪个分支、哪个提交，是否在基线分支上
- 换机器、换 `repos/` 位置后确认 `--repos-dir` / `GFV_REPOS_DIR` / 单仓环境变量指向对

### 不适用（不要用本 skill）

- 指望它克隆、fetch 或切分支——`fix` 与 `next_step` 只是工单，交给安装类 skill（规划中）
- velox 的 C++ 依赖、setup 脚本要装的源码级库 → `flink-velox-cpp-deps-check`
- 编译工具链 / 构建辅助工具 → `flink-velox-toolchain-check` / `flink-velox-build-tools-check`
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`

## 检查项

| 仓 | 上游 | 基线分支 | 标志文件（缺任一即不可用） | 单仓指定 |
|---|---|---|---|---|
| velox | `https://github.com/bigo-sg/velox.git` | `gluten-20260829` | `CMakeLists.txt`、`scripts/` | `VELOX_HOME` |
| velox4j | `https://github.com/bigo-sg/velox4j.git` | `gluten-20260829` | `pom.xml` | `VELOX4J_HOME` |
| gluten | `https://github.com/apache/gluten.git` | `main` | `gluten-flink/pom.xml` | `GLUTEN_HOME` |

- 每个仓：目录存在 → 标志文件齐全 → `git -C <仓> rev-parse --abbrev-ref HEAD` / `rev-parse --short HEAD` / `remote get-url origin` 读分支、HEAD、远端
- 分支 ≠ 基线：条目仍为 ok，detail 注明 `; baseline branch is <基线>`，`on_baseline: false`——只提示不判负
- git 不在 PATH 或目录不是 git checkout：条目为 warn（分支与 HEAD 读不到），不阻断

## 执行步骤

### 步骤 1：定位置、扫描

- 仓库根按下列顺序定位，定位不到才需要显式给：`--repos-dir` → `GFV_REPOS_DIR` → 从当前目录向上找含任一仓的 `repos/`；单个仓可用 `VELOX_HOME` / `VELOX4J_HOME` / `GLUTEN_HOME` 单独指定（优先于仓库根）

```bash
python3 <skill_dir>/scripts/source_repos_check.py [--repos-dir DIR] [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `source-repos OK       velox gluten-20260829 @ 0123abc (/work/repos/velox); velox4j …; gluten main @ …`；缺了逐项 `FAIL <仓>: …` 并附 `fix:`（完整的 `git clone -b <基线> <上游>` 命令）；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/source-repos-check.json`（留档规则见「环境约定」）。

退出码：**0** 三个仓都在且可用（warn 允许）；**1** 有缺失或不可用；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——缺的仓给出一句完整的克隆命令（含基线分支与目标目录），不可用的给出重克隆；都在时提示偏离基线的仓。
2. `on_baseline: false` 不是错误：可能是有意切到别的分支做实验，但要转述给用户——基线之外的分支编出来的栈没经过验证。
3. warn（git 不在 PATH、不是 git checkout）不阻断，但意味着"在哪个分支"无法回答，要转述。
4. 本 skill 不克隆、不切分支：把 `next:` 交给安装类 skill（规划中），做完重扫直到 READY。

## 环境约定

- **只读**：只查目录与标志文件存在、运行 `git -C <仓> rev-parse` / `remote get-url`（不 fetch）；不写任何文件（`--out` 指定的除外）
- `ENV_CHECK_WALK_ROOT`（给向上找 `repos/` 的搜索划上界，到该目录即停）仅供 tier-A 用例构造假机器，正常使用不要设置
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`root`、`checkouts`、`missing`、`next_step`、`error_message` 必须保留（下例缺 velox4j、gluten 不在 git 下，节选）：

```json
{
  "flink_velox_source_repos_check_result": {
    "success": false,
    "checked_at": "2026-11-24T10:00:00+08:00",
    "status": "fail",
    "root": "/work/repos",
    "root_via": "GFV_REPOS_DIR",
    "checkouts": {
      "velox": {"path": "/work/repos/velox", "branch": "gluten-20260829", "head": "8923c2297", "origin": "https://github.com/bigo-sg/velox.git", "baseline_branch": "gluten-20260829", "on_baseline": true, "pinned_via": null},
      "velox4j": {"path": "/work/repos/velox4j", "branch": null, "head": null, "origin": null, "baseline_branch": "gluten-20260829", "on_baseline": null, "pinned_via": null, "error": "missing"},
      "gluten": {"path": "/work/repos/gluten", "branch": null, "head": null, "origin": null, "baseline_branch": "main", "on_baseline": null, "pinned_via": null}
    },
    "items": [
      {"name": "velox", "status": "ok", "detail": "gluten-20260829 @ 8923c2297 (/work/repos/velox)", "on_baseline": true},
      {"name": "velox4j", "status": "fail", "detail": "no velox4j checkout at /work/repos/velox4j", "fix": "git clone -b gluten-20260829 https://github.com/bigo-sg/velox4j.git into /work/repos, or set VELOX4J_HOME"},
      {"name": "gluten", "status": "warn", "detail": "/work/repos/gluten: not a git checkout, branch and HEAD not read"}
    ],
    "missing": ["velox4j: no velox4j checkout at /work/repos/velox4j"],
    "next_step": "clone the missing checkouts (git clone -b gluten-20260829 https://github.com/bigo-sg/velox4j.git) into /work/repos, then rerun",
    "error_message": "1 item(s) missing: velox4j"
  }
}
```

- 原始观测：`root` / `root_via`（`--repos-dir` / `GFV_REPOS_DIR` / `repos/ walked up from the working directory`）、`checkouts.<仓>`（`path` / `branch` / `head` / `origin` / `baseline_branch` / `on_baseline` / `pinned_via`，缺失或不可用时另有 `error`）；`items` 是逐项判定（`name` / `status` / `detail`，fail 另带 `fix`，ok 的带 `on_baseline`），`status` 是全部条目的最差值
- `success`：没有 fail。`missing`：fail 条目，形如 `velox4j: no velox4j checkout at …`
- 失败形态：`success: false`，`error_message` 形如 `1 item(s) missing: velox4j`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头

## 边界与相邻 skill

- `flink-velox-cpp-deps-check`：velox 自带 setup 脚本里的源码级 C++ 库清单——它要的 velox checkout 正是本 skill 核对的那个
- `flink-velox-toolchain-check`、`flink-velox-build-tools-check`：编译侧的兄弟检查项，触发词不重叠
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 克隆、fetch、切分支：同族安装类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
