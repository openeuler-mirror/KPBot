---
name: flink-velox-build-tools-check
description: >
  Flink + GlutenFlink（velox 向量化栈）构建辅助工具只读检查：在 PATH 上找 ninja、ccache、autoconf、
  automake、libtool、flex、bison、python3、patchelf 九件并取各自版本，逐项 ok/fail，缺的给 openEuler
  包名（ninja → ninja-build），末尾唯一 next_step。只扫不改：不装包、不改环境。适用于搭 GlutenFlink 构建环境
  前后、velox 构建报 ninja/patchelf/flex 缺失时先查缺什么。触发词：构建工具、build tools、ninja 装了没、
  ccache、patchelf、flex bison、autotools。不负责：git/cmake/gcc/g++/mvn 编译工具链 →
  flink-velox-toolchain-check；C++ 库头文件 → flink-velox-cpp-deps-check；"能不能编译 GlutenFlink 栈"的总裁决
  → flink-velox-env-check；装包修复 → 同族安装类 skill。
compatibility: Linux (aarch64/x86_64)，Python 3.7+ 标准库
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: bigdata
  side_effect_free: "true"
  version: "1.0.0"
---

# Flink-velox 构建辅助工具检查

velox 及其第三方依赖的构建除了编译器，还要 ninja（velox4j 的 CMake 后端）、ccache（重编加速）、autoconf / automake / libtool（部分第三方库的 autotools 构建）、flex / bison（生成器）、python3（构建脚本）、patchelf（修 rpath）。缺哪件都在构建半途才炸，报错离"少装了一个包"很远。本 skill 只做一件事：**把这九件逐个在 PATH 上找一遍、取版本，缺的说清该装哪个包**。它是 `flink-velox-env` 检查家族里"构建辅助工具"这一具体项：只读、不改动机器、不装包。探测表与判据细节在 [probe-table.md](references/probe-table.md)。

## 术语

这套栈的官方叫法是 GlutenFlink；项目名 GFVBot（本 skill 的包名映射解析自其安装器的 build-tools 探测清单）按原名保留，`GFV` 与 GlutenFlink 指同一生态。

## 使用场景

### 适用（应使用本 skill）

- 搭 GlutenFlink 构建环境之前或之后，确认构建辅助工具齐不齐
- velox / 第三方库构建报 `ninja: command not found`、`patchelf: not found`、`flex: command not found` 时先查缺什么
- 换机器、换容器后的构建工具确认
- 要知道这台机器 ninja / ccache 等工具的版本

### 不适用（不要用本 skill）

- 指望它把缺的装上——`fix` 与 `next_step` 只是工单，修复交给同族的安装类 skill
- git / cmake / gcc / g++ / mvn → `flink-velox-toolchain-check`
- C++ 库头文件（libevent / openssl / re2 …）→ `flink-velox-cpp-deps-check`
- "这台机器能不能编译 GlutenFlink 栈"的总裁决 → 统筹 `flink-velox-env-check`（它按目标聚合各具体项检查）

## 检查项

| 工具 | 用途 | openEuler 包 | 怎么判 |
|---|---|---|---|
| ninja | velox4j 的 CMake 构建后端 | **ninja-build**（与工具名不同） | PATH 上找得到，`ninja --version` 取版本 |
| ccache | 重编加速 | ccache | 同上 |
| autoconf | 第三方库 autotools 构建 | autoconf | 同上 |
| automake | 同上 | automake | 同上 |
| libtool | 同上 | libtool | 同上 |
| flex | 生成器 | flex | 同上 |
| bison | 生成器 | bison | 同上 |
| python3 | 构建脚本 | python3 | 同上 |
| patchelf | 修共享库 rpath | patchelf | 同上 |

版本取 `--version` 输出里第一个 `x.y.z`；取不到时用整行，什么都没输出时记 `version unknown`（路径仍在 detail 里）。**版本只报告，不设门槛**。

## 执行步骤

### 步骤 1：扫描

```bash
python3 <skill_dir>/scripts/build_tools_check.py [--json] [--out FILE [--overwrite]]
```

输出是人读报告：齐了一行 `build-tools OK       all 9 present: ninja 1.11.1, ccache 4.8.3, ...`；缺了逐项 `FAIL <tool>: not found on PATH` 并附 `fix:`；末尾是 `READY` / `NOT READY` 与 `next:`。要机器消费时加 `--json`（只输出 JSON），要留档再加 `--out <项目内路径>/build-tools-check.json`（留档规则见「环境约定」）。

退出码：**0** 九件全在；**1** 有缺失；**2** 检查本身出错（`error_message` 以 `check aborted:` 开头，此时结论不可信，先看报错）、`--out` 被拒绝（见「环境约定」）或命令行参数非法（stderr 为用法提示，无 JSON）。

### 步骤 2：判读

1. 先看 `next:`——缺什么就是一句 `dnf install <缺的包名>`。
2. `fix` 里的包名是 openEuler 的（只有 ninja → `ninja-build` 与工具名不同）；其它发行版按同名或对应名换算。
3. 本 skill 不装包：把 `next:` 交给同族安装类 skill，装完重扫直到 READY。

## 环境约定

- **只读**：只在 PATH 上找可执行文件并运行各工具的 `--version`；不写任何文件（`--out` 指定的除外）
- `--version` 的退出码不看：有些工具把版本打到 stderr 或以非零退出，仍取第一行作版本
- `--out` 留档受限：目标必须是项目目录（即运行时的当前工作目录）内的常规路径——项目目录之外、符号链接（含指向项目内的）一律拒绝；已存在的文件不覆盖，除非显式加 `--overwrite`；被拒绝时退出码 2、stderr 说明原因，检查结论照常打印

## 输出

`--json` 打印的即契约；对话里汇报时给出同一结构，`success`、`status`、`items`、`missing`、`next_step`、`error_message` 必须保留（下例缺 ninja）：

```json
{
  "flink_velox_build_tools_check_result": {
    "success": false,
    "checked_at": "2026-11-23T11:20:00+08:00",
    "status": "fail",
    "tools": {
      "ninja": {"path": null, "version": null, "package": "ninja-build"},
      "ccache": {"path": "/usr/bin/ccache", "version": "4.8.3", "package": "ccache"}
    },
    "items": [
      {"name": "ninja", "status": "fail", "detail": "not found on PATH", "fix": "system package (openEuler name): ninja-build", "package": "ninja-build"},
      {"name": "ccache", "status": "ok", "detail": "4.8.3 (/usr/bin/ccache)", "version": "4.8.3", "package": "ccache"}
    ],
    "missing": ["ninja: not found on PATH"],
    "next_step": "install the missing build tools (dnf install ninja-build), then rerun",
    "error_message": "1 tool(s) missing: ninja"
  }
}
```

- `tools`：每个工具的原始观测（`path`、`version`、`package`）；`items`：逐项判定（`name` / `status` / `detail`，fail 另带 `fix`）。条目状态只有 `ok` / `fail`；`status` 是全部条目的最差值
- `success`：九件全在。`missing`：缺的工具，形如 `ninja: not found on PATH`
- 失败形态：`success: false`，`error_message` 形如 `1 tool(s) missing: ninja`；检查崩溃：退出码 2，JSON 仍是完整契约形态（`status: fail`、空观测与条目、`missing: []`、`next_step` 指向先修检查错误），`error_message` 以 `check aborted:` 开头，`success: false`

## 边界与相邻 skill

- `flink-velox-toolchain-check`：git / cmake / gcc / g++ / mvn——同层级的兄弟检查项，触发词不重叠
- `flink-velox-cpp-deps-check`：C++ 库头文件（包管理器级与 Velox 源码级）
- 统筹 `flink-velox-env-check`：按 goal 聚合各具体项检查给总裁决；本 skill 的输出是它的输入之一
- 装包修复：同族安装类 skill（规划中，后续 PR）；本 skill 的 `next_step` 与每项 `fix` 即交给它们的工单，本 skill 不执行任何修复
