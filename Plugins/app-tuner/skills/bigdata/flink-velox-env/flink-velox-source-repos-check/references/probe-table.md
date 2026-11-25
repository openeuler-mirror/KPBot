# 探测表与判据：源码仓

脚本 `scripts/source_repos_check.py` 的判读依据。条目名即仓名。

## 1. 仓库根定位

`--repos-dir` → `$GFV_REPOS_DIR` → 从当前目录向上找 `repos/`，首个命中者。显式给出的 `--repos-dir` / `$GFV_REPOS_DIR` 是目标指定：目录存在即直接作为根，是否含仓交给逐仓条目判定（detail / fix 指向该确切路径）；目录不存在才落到下一候选，`root_via` 记录实际命中者。向上查找的 `repos/` 须含 velox / velox4j / gluten 至少一个目录，默认到文件系统根，设置了 `ENV_CHECK_WALK_ROOT`（tier-A 缝）时走到该目录即停。单个仓可用 `VELOX_HOME` / `VELOX4J_HOME` / `GLUTEN_HOME` 单独指定，优先于仓库根（JSON 的 `pinned_via` 记录）。

## 2. 每个仓的探测与判定

| 情形 | status | detail | fix |
|---|---|---|---|
| 目录不存在 | fail | `no <仓> checkout at <路径或 <repos dir>/<仓>>` | `git clone -b <基线> <上游> into <根>`（无根时 `pass --repos-dir or set GFV_REPOS_DIR`）`, or set <单仓环境变量>` |
| 标志文件缺失 | fail | `<路径> is not a usable <仓> checkout (missing <标志…>)` | `git clone -b <基线> <上游> afresh, or point --repos-dir (or <单仓环境变量>) at a complete checkout` |
| git 不在 PATH | warn | `<路径>: git is not on PATH, branch and HEAD not read` | — |
| 不是 git checkout（rev-parse 失败） | warn | `<路径>: not a git checkout, branch and HEAD not read` | — |
| 读到 HEAD | ok | `<分支> @ <短 HEAD> (<路径>)`，分支 ≠ 基线时追加 `; baseline branch is <基线>` | — |

| 仓 | 标志文件 | 基线分支 | 上游 | 单仓环境变量 |
|---|---|---|---|---|
| velox | `CMakeLists.txt`、`scripts/` | gluten-20260829 | https://github.com/bigo-sg/velox.git | VELOX_HOME |
| velox4j | `pom.xml` | gluten-20260829 | https://github.com/bigo-sg/velox4j.git | VELOX4J_HOME |
| gluten | `gluten-flink/pom.xml` | main | https://github.com/apache/gluten.git | GLUTEN_HOME |

- git 以固定 argv 调用（`-C <仓> rev-parse --abbrev-ref HEAD` / `rev-parse --short HEAD` / `remote get-url origin`），10 秒超时，不 fetch；`origin` 脱敏后输出：去掉 userinfo，去掉名字含 token / secret / password / credential 的查询参数
- `on_baseline`：分支 == 基线 → true，否则 false（条目仍 ok）；读不到 HEAD 时为 null
- `status` = 全部条目的最差值；`success` = 没有 fail；`missing` = `"仓: detail"` 列表

## 3. next_step

按"缺的 → 不可用的 → 都在"取第一个成立的：
1. 有缺失 → `clone the missing checkouts (<按表序的 git clone -b <基线> <上游>; …) into <根>`（无根时 ` (pass --repos-dir or set GFV_REPOS_DIR)`）`, then rerun`
2. 有不可用 → `re-clone the unusable checkouts (<仓名…>), then rerun`
3. 都在 → `ready: the three source checkouts are in place at <根>`，有 warn 追加 `; warnings above`，有偏离基线的追加 `; not on the baseline branch: <仓名…>`

## 4. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头

## 5. 来源

三个仓、上游、基线分支与标志文件来自 GFVBot 安装器的 `GFVBOT_REPOS` 表与构建脚本读取的三个源码路径（velox / velox4j / gluten）；"偏离基线只提示不判负"与安装器一致（它也只记录分支，不强制切换）。
