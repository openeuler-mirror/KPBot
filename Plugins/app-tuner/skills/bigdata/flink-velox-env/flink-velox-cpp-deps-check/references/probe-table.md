# 探测表与判据：C++ 依赖

脚本 `scripts/cpp_deps_check.py` 的判读依据。条目名即库名；第二层另有汇总条目 `source-deps`。

## 1. 第一层：包管理器级（13 个库）

| 库 | 头文件候选（任一） | openEuler 包 |
|---|---|---|
| libevent | `event2/event.h` | libevent-devel |
| openssl | `openssl/ssl.h` | openssl-devel |
| re2 | `re2/re2.h` | re2-devel |
| zstd | `zstd.h` | zstd-devel |
| lz4 | `lz4.h` | lz4-devel |
| double-conversion | `double-conversion/double-conversion.h` | double-conversion-devel |
| libdwarf | `libdwarf.h`、`libdwarf-0/libdwarf.h`、`libdwarf/libdwarf.h` | libdwarf-devel |
| libelf | `libelf.h` | **elfutils-libelf-devel** |
| curl | `curl/curl.h` | libcurl-devel |
| icu | `unicode/uversion.h` | libicu-devel |
| sodium | `sodium.h` | **libsodium-devel** |
| zlib | `zlib.h` | zlib-devel |
| librdkafka | `librdkafka/rdkafka.h` | librdkafka-devel |

- 探测目录：`/usr/include/`、`/usr/include/<triplet>/`（**只取目标编译器**的 multiarch 目录：PATH 上有 gcc 时 `gcc -dumpmachine`，否则按 `platform.machine()` 查表 aarch64 → aarch64-linux-gnu、x86_64 → x86_64-linux-gnu、armv7l → arm-linux-gnueabihf、ppc64le → powerpc64le-linux-gnu、riscv64 → riscv64-linux-gnu、s390x → s390x-linux-gnu；JSON 的 `triplet` 字段记录实际取值）、`/usr/local/include/`。其它架构的目录不探：x86_64 主机上只有 aarch64-linux-gnu/curl/curl.h 时 curl 判缺
- ok：detail `header found: <找到的路径>`；fail：detail `no header <第一个候选> under /usr/include or /usr/local/include`，fix `system package (openEuler name): <包名>`

## 2. 第二层：Velox 源码级

1. **选脚本**（读 `/etc/os-release` 的 `ID` / `VERSION_ID`）：ubuntu/debian → `scripts/setup-ubuntu.sh`；centos/rhel 7 → 没有（warn：不是受支持的构建主机）；其余 → `scripts/setup-centos9.sh`
2. **解析清单**：读脚本，从 `function install_velox_deps {` 行起到第一个以 `}` 开头的行止，取 `run_and_time install_<lib>` 行，去掉 `<lib>` 以 `_from_dnf` / `_from_apt` 结尾和等于 `conda` 的条目，保持顺序
3. **逐库探测** `/usr/local` 下任一：`lib/cmake/<C>` 或 `lib64/cmake/<C>`（C 取 lib、首字母大写、全大写三种）；`include/<lib>/`；`include/<lib>.h` 或 `include/lib<lib>.h`；探针二进制（protobuf → `bin/protoc`，表 `SOURCE_PROBE_OVERRIDE`）
4. 汇总条目 `source-deps`：全在 ok `<n>/<n> present under /usr/local (list: <脚本>; velox checkout via <来源>)`；有缺 info `<present>/<n> present (...)`；缺的库各一条 fail，fix `built by Velox's own setup script: install_<lib>()`

| 第二层情形 | source-deps 状态 | 说明 |
|---|---|---|
| 没有 velox checkout（四种定位都没中） | skipped | `needs the velox checkout — its setup script is the library list (give --velox-dir or VELOX_HOME)` |
| `--velox-dir` / `VELOX_HOME` 指向的路径不是目录 | warn | `<路径> is not a directory` |
| CentOS/RHEL 7 | warn | checkout 不带适配脚本 |
| 脚本文件不存在 | warn | `<路径> not found` |
| 脚本里解析不到 install 调用 | warn | `no install_* calls found in install_velox_deps inside <路径>` |

## 3. velox checkout 定位顺序

`--velox-dir` → `VELOX_HOME` → `GFV_REPOS_DIR/velox` → 从当前目录向上找 `repos/velox`。前两项给了但不是目录时报 warn 并跳过第二层；后两项找不到则静默继续。向上查找默认到文件系统根；设置了 `ENV_CHECK_WALK_ROOT`（tier-A 缝）时走到该目录即停。

## 4. 判定与 next_step

- `success` = 没有 fail（第二层 skipped / warn 不阻断）；`status` = 全部条目最差值
- `next_step`：系统库缺 → `install the missing system packages (dnf install <包名…按表序>), then rerun`；只源码级缺 → `build the missing libraries with Velox's own setup script (install_<lib>(), …), then rerun`；第二层 skipped → `system headers present; source-level libraries not probed — give the velox checkout (--velox-dir or VELOX_HOME) to check them`；第二层 warn → `system headers present; source-level libraries not probed (<原因>)`；两层都探过且全在 → `ready: every C++ library the stack needs is present`
- `error_message`：失败形如 `2 item(s) missing: openssl, fmt`（先系统库后源码级库）

## 5. --out 留档与中止态

- `--out FILE` 的目标必须是项目目录（运行时的当前工作目录）内的常规路径：`realpath` 落在项目目录之外、或路径本身是符号链接 → 拒绝（退出码 2，stderr 说明），检查结论照常打印
- 已存在的文件不覆盖，除非加 `--overwrite`；新建用独占创建（`open(..., 'x')`），不跟随符号链接
- 检查崩溃（异常）：退出码 2，JSON 仍是完整契约形态——`success: false`、`status: fail`、空的观测 / 条目 / `missing`、`next_step: fix the check error first (see error_message), then rerun`、`error_message` 以 `check aborted: ` 开头——调用方按契约处理故障时不会再遇到缺字段

## 6. 来源

13 个库与包名表来自 GFVBot 安装器的 cpp-deps 探测清单；源码级清单的解析方式来自其 `source-deps.sh`（逐库探测后驱动官方 install 函数）。本 skill 只探测，不驱动任何安装。
