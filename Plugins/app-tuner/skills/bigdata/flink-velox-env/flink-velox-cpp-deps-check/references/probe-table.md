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

- 探测目录：`/usr/include/`、`/usr/include/*/`（triplet 子目录，按名排序）、`/usr/local/include/`
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

`--velox-dir` → `VELOX_HOME` → `GFV_REPOS_DIR/velox` → 从当前目录向上找 `repos/velox`。前两项给了但不是目录时报 warn 并跳过第二层；后两项找不到则静默继续。

## 4. 判定与 next_step

- `success` = 没有 fail（第二层 skipped / warn 不阻断）；`status` = 全部条目最差值
- `next_step`：系统库缺 → `install the missing system packages (dnf install <包名…按表序>), then rerun`；只源码级缺 → `build the missing libraries with Velox's own setup script (install_<lib>(), …), then rerun`；全在 → `ready: every C++ library the stack needs is present`
- `error_message`：失败形如 `2 item(s) missing: openssl, fmt`（先系统库后源码级库）

## 5. 来源

13 个库与包名表来自 GFV 安装器的 cpp-deps 探测清单；源码级清单的解析方式来自其 `source-deps.sh`（逐库探测后驱动官方 install 函数）。本 skill 只探测，不驱动任何安装。
