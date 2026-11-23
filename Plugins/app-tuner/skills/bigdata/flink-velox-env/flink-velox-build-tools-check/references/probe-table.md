# 探测表与判据：构建辅助工具

脚本 `scripts/build_tools_check.py` 的判读依据。条目名即工具名。

## 1. 探测

| 工具 | 用途 | 版本怎么取 | openEuler 包 |
|---|---|---|---|
| ninja | velox4j 的 CMake 构建后端 | `1.11.1`（整行即版本） | **ninja-build**（与工具名不同） |
| ccache | 重编加速 | `ccache version 4.8.3` → 4.8.3 | ccache |
| autoconf | 第三方库 autotools 构建 | `autoconf (GNU Autoconf) 2.71` → 2.71 | autoconf |
| automake | 同上 | `automake (GNU automake) 1.16.5` → 1.16.5 | automake |
| libtool | 同上 | `libtool (GNU libtool) 2.4.7` → 2.4.7 | libtool |
| flex | 生成器 | `flex 2.6.4` → 2.6.4 | flex |
| bison | 生成器 | `bison (GNU Bison) 3.8.2` → 3.8.2 | bison |
| python3 | 构建脚本 | `Python 3.11.6` → 3.11.6 | python3 |
| patchelf | 修共享库 rpath | `patchelf 0.15.0` → 0.15.0 | patchelf |

- 工具以固定 argv 调用（不经 shell），15 秒超时，stdout 与 stderr 合并取第一行
- 第一行里没有 `x.y.z` 时用整行作版本；什么都没输出时 `version: null`、detail 写 `version unknown (<path>)`
- `--version` 的退出码不看：版本打到 stderr 或非零退出的工具仍能取到版本

## 2. 判定

| 情形 | status | detail | fix |
|---|---|---|---|
| PATH 上找到 | ok | `<version> (<path>)` | — |
| PATH 上找不到 | fail | `not found on PATH` | `system package (openEuler name): <包名>` |

- `status` = 全部条目的最差值（本 skill 只会出现 ok / fail）
- `success` = 没有 fail；`missing` = `"<工具>: not found on PATH"` 列表
- `error_message`：成功为空串；失败形如 `2 tool(s) missing: ninja, patchelf`

## 3. next_step

- 全在：`ready: the build tools are complete`
- 有缺：`install the missing build tools (dnf install <包名…按表序>), then rerun`——一句命令把缺的装齐，交给同族安装类 skill 执行

## 4. 为什么不设版本门槛

velox 对 ninja、flex、bison 等的最低要求随上游版本变化；本 skill 只报告版本，门槛交给构建时的真实报错，不猜。

## 5. 来源

九件与包名映射来自 GFV 安装器的 build-tools 探测清单（ninja ccache autoconf automake libtool flex bison python3 patchelf）；openEuler 包名以 `ninja → ninja-build` 一处为准，其余同名。
