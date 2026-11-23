---
name: flink-velox-cpp-deps-check
description: >
  Flink + GlutenFlink（velox 向量化栈）C++ 依赖只读检查，两层：包管理器级 13 个库按头文件在
  /usr/include（含 <triplet>/ 子目录）与 /usr/local/include 探测，缺的给 openEuler -devel 包名；
  源码级库清单解析自 velox checkout 自带 setup 脚本的 install_velox_deps 调用序列，按 /usr/local 下的
  cmake 配置目录 / include / 头文件 / 探针二进制探测。逐项 ok/fail，末尾唯一 next_step。只扫不改：
  不装包、不编译。适用于搭 GFV 构建环境前后、编译报 fatal error: xxx.h: No such file 时先查缺什么。
  触发词：C++ 依赖、头文件、缺哪个 .h、No such file or directory、devel 包、libevent/openssl/re2 在不在。
  不负责：git/cmake/gcc/g++/mvn → flink-velox-toolchain-check；ninja/ccache/flex 等构建辅助工具 →
  flink-velox-build-tools-check；"能不能编译 GFV 栈"的总裁决 → flink-velox-env-check；装包与跑 setup 脚本
  → 同族安装类 skill。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库；包名按 openEuler 给出
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink-velox C++ 依赖检查

velox 及 gluten-flink 原生部分依赖一批 C++ 库：一半来自发行版包管理器（libevent、openssl、re2、zstd、lz4 …），另一半由 velox 自己的 setup 脚本从源码构建装进 `/usr/local`（boost/folly 链、protobuf、arrow …）。缺哪个，编译都在半途才炸成一句 `fatal error: xxx.h: No such file or directory`，看不出还缺几个。本 skill 只做一件事：**把这批库按两层逐个探测一遍，缺的说清该装哪个包、该跑哪个安装函数**。它是 `flink-velox-env` 检查家族里"C++ 依赖"这一具体项：只读、不改动机器、不装包、不编译。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 使用场景

### 适用（应使用本 skill）

- 搭 GFV 构建环境之前或之后，确认 C++ 依赖齐不齐
- 编译报 `fatal error: event2/event.h: No such file`、`re2/re2.h: No such file` 等时先查缺什么
- 换机器、换容器后的依赖确认
- 要知道 velox 源码级库里哪几个还没装进 `/usr/local`

### 不适用（不要用本 skill）

- 指望它把缺的装上或跑 setup 脚本——`fix` 与 `next_step` 只是工单，修复交给同族的安装类 skill
- git / cmake / gcc / g++ / mvn → `flink-velox-toolchain-check`
- ninja / ccache / autoconf / flex / bison / patchelf 等 → `flink-velox-build-tools-check`
- "这台机器能不能编译 GFV 栈"的总裁决 → 统筹 `flink-velox-env-check`（它按目标聚合各具体项检查）

## 检查项

### 第一层：包管理器级（13 个库）

| 库 | 探测头文件 | openEuler 包 |
|---|---|---|
| libevent | `event2/event.h` | libevent-devel |
| openssl | `openssl/ssl.h` | openssl-devel |
| re2 | `re2/re2.h` | re2-devel |
| zstd | `zstd.h` | zstd-devel |
| lz4 | `lz4.h` | lz4-devel |
| double-conversion | `double-conversion/double-conversion.h` | double-conversion-devel |
| libdwarf | `libdwarf.h`、`libdwarf-0/libdwarf.h`、`libdwarf/libdwarf.h` | libdwarf-devel |
| libelf | `libelf.h` | elfutils-libelf-devel |
| curl | `curl/curl.h` | libcurl-devel |
| icu | `unicode/uversion.h` | libicu-devel |
| sodium | `sodium.h` | libsodium-devel |
| zlib | `zlib.h` | zlib-devel |
| librdkafka | `librdkafka/rdkafka.h` | librdkafka-devel |

头文件在 `/usr/include`、`/usr/include/<triplet>/`（如 `aarch64-linux-gnu/`）、`/usr/local/include` 下任一处找到即 ok。

### 第二层：Velox 源码级（清单来自 velox checkout）

- 清单**不写死**：从 velox checkout 自带 setup 脚本的 `install_velox_deps` 函数里解析 `run_and_time install_<lib>` 调用序列，去掉包管理器与 conda 条目
- 按本机发行版选脚本：Ubuntu/Debian → `setup-ubuntu.sh`；CentOS/RHEL 7 → 没有（warn，不是受支持的构建主机）；其余（含 openEuler、CentOS 9）→ `setup-centos9.sh`
- 每个库在 `/usr/local` 下任一处找到即 ok：`lib/cmake/<Name>` 或 `lib64/cmake/<Name>`（name / Capitalized / UPPER 三种写法）、`include/<lib>/`、`include/<lib>.h` 或 `include/lib<lib>.h`、探针二进制（protobuf → `bin/protoc`）
- 没有 velox checkout 时这一层为 **skipped**，不判负

## 执行步骤

### 步骤 1：定位置、扫描

- velox checkout 按下列顺序自动定位，定位不到才需要显式给：`--velox-dir` → `VELOX_HOME` → `GFV_REPOS_DIR/velox` → 从当前目录向上找 `repos/velox`

```bash
python3 <skill_dir>/scripts/cpp_deps_check.py [--velox-dir DIR] [--json] [--out FILE]
```

输出是人读报告：齐了一行 `cpp-deps    OK       13/13 system headers present; source-level 17/17 present under /usr/local (list: …)`；缺了逐项 `FAIL <lib>: …` 并附 `fix:`；第二层没探时写明原因；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/cpp-deps-check.json`。

退出码：**0** 探到的库全在（第二层 skipped 也算）；**1** 有缺失；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——系统库缺失是一句 `dnf install <缺的 -devel 包名>`；源码级库缺失是对应的 `install_<lib>()` 安装函数。
2. 第一层 `fix` 里的包名是 openEuler 的（`libelf` → `elfutils-libelf-devel`、`sodium` → `libsodium-devel` 与库名不同）；其它发行版按对应名换算。
3. 第二层 `skipped` 不是问题：说明没给 velox checkout；要探就给 `--velox-dir`。`warn`（如 CentOS 7 没有 setup 脚本、脚本里解析不到 install 调用）要转述给用户。
4. 本 skill 不装包、不跑 setup 脚本：把 `next:` 交给同族安装类 skill，做完重扫直到 READY。

## 环境约定

- **只读**：只查文件存在与可执行位，读 `/etc/os-release` 与 velox 的 setup 脚本文本；不写任何文件（`--out` 指定的除外）
- 头文件探测只看文件在不在，不看版本与内容；版本门槛交给构建时的真实报错
- `ENV_CHECK_SYSROOT` 仅供 tier-A 用例构造假机器（给所有绝对系统路径加前缀），正常使用不要设置
- `--out` 的落盘文件放项目目录内，不写 `/tmp`

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`status`、`system`、`source`、`missing`、`next_step`、`error_message` 必须保留（下例缺 openssl 与源码级 fmt，节选）：

```json
{
  "flink_velox_cpp_deps_check_result": {
    "success": false,
    "checked_at": "2026-11-23T11:20:00+08:00",
    "status": "fail",
    "os": {"id": "openeuler", "version": "24.03"},
    "system": {"expected": 13, "present": 12, "missing": ["openssl"]},
    "source": {"list_source": "/work/repos/velox/scripts/setup-centos9.sh", "expected": ["gflags", "glog", "protobuf", "fmt"], "present": 3, "missing": ["fmt"]},
    "velox_dir": "/work/repos/velox",
    "items": [
      {"name": "libevent", "status": "ok", "detail": "header found: /usr/include/event2/event.h"},
      {"name": "openssl", "status": "fail", "detail": "no header openssl/ssl.h under /usr/include or /usr/local/include", "fix": "system package (openEuler name): openssl-devel", "package": "openssl-devel"},
      {"name": "fmt", "status": "fail", "detail": "not found under /usr/local (no cmake config dir, include dir, header or probe binary)", "fix": "built by Velox's own setup script: install_fmt()"},
      {"name": "source-deps", "status": "info", "detail": "3/4 present (list: /work/repos/velox/scripts/setup-centos9.sh; velox checkout via --velox-dir)"}
    ],
    "missing": ["openssl: no header openssl/ssl.h under /usr/include or /usr/local/include", "fmt: not found under /usr/local"],
    "next_step": "install the missing system packages (dnf install openssl-devel), then rerun",
    "error_message": "2 item(s) missing: openssl, fmt"
  }
}
```

- `system` / `source`：两层的原始观测（`expected` / `present` / `missing`，源码级另有 `list_source` 即解析出的脚本路径）；`velox_dir`：定位到的 checkout（没有则 null）
- `items`：逐项判定（`name` / `status` / `detail`，fail 另带 `fix`）；`source-deps` 一条是第二层的汇总（ok / info / skipped / warn），齐全的源码级库不单独出条目。`status` 是全部条目的最差值——第二层 skipped / warn 时 `status` 相应为 skipped / warn，但 `success` 仍为 true
- `success`：没有 fail（第二层 skipped / warn 不阻断）。`missing`：形如 `openssl: no header …` 与 `fmt: not found under /usr/local`
- 失败形态：`success: false`，`error_message` 形如 `2 item(s) missing: openssl, fmt`；检查崩溃：退出码 2，`error_message` 以 `check aborted:` 开头，`success: false`
- `next_step` 先系统库后源码级库，一次只给一个动作；两层都缺时先补系统库再重扫

## 边界与相邻 skill

- `flink-velox-toolchain-check`、`flink-velox-build-tools-check`：同层级的兄弟检查项（编译工具链、构建辅助工具），触发词不重叠
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 装包与跑 velox setup 脚本：同族安装类 skill；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
