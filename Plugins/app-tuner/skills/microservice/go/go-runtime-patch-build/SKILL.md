---
name: go-runtime-patch-build
description: 当需要给 Go runtime 打私有补丁、在隔离构建根目录构建工具链、重编译 Go 服务、做二进制等价门控和回退时使用。输出新工具链/二进制身份、等价门控结果和回退证据。
---

# Go Runtime Patch Build

本 skill 只负责“把 Go runtime 补丁安全地构建出来并替换到服务二进制”，不定义补丁本身的行为、开关键名或业务语义。具体补丁由调用方以外部文件提供。

## 何时触发

- 需要修改 Go runtime 源码并重新构建 Go 工具链。
- 需要用新工具链重编译业务 Go 服务，并保持基线工具链可回退。
- 需要做二进制身份、构建参数和回退路径的等价门控。

不进入本 skill：

- 只需要改业务代码或普通编译参数（走 code-optimizer / compiler-optimization）。
- 没有补丁文件或无法记录 Go 基线。
- 不允许重编译工具链或业务二进制。

## 必读 Reference

- `references/go-runtime-patch-playbook.md` — 基线记录、隔离构建根目录、补丁校验/应用、`make.bash`、服务重编译、二进制等价和回退。

## 输入

- 基线 Go：`go version`、`$GOROOT/VERSION`、`git describe`、源码包 sha256、基线 `bin/go` sha256。
- 补丁：外部 `--patch-file` 或 `KPBOT_GO_RUNTIME_PATCH`，补丁 hash。
- 业务服务：源码 commit、构建命令、依赖版本、启动参数、配置、绑核和部署形态。
- 授权：重编译工具链、替换二进制、重启服务的批准。

## Workflow

### Step 0：基线记录

- 记录基线 `GOROOT`、`VERSION`、`git describe`、源码包 hash 和 `bin/go` sha256。
- 记录业务二进制 `go version -m`、sha256、`/proc/<pid>/exe`、启动参数和环境变量。
- 基线不完整时不进入构建。

### Step 1：隔离构建根目录

- 复制基线 Go 源码树到独立 `BUILD_ROOT`，不要修改基线 `$GOROOT`。
- 删除/覆盖前校验 `BUILD_ROOT` 的 realpath 非空、非 `/`、非 `$GOROOT`、非用户数据目录，且不在符号链接边界外；只清理本轮记录并带标记的目录。
- 校验补丁与当前 Go 基线匹配；不匹配时停止，不做模糊套用。

### Step 2：构建工具链

- 在 `BUILD_ROOT/src` 执行 `make.bash`（Windows 用 `make.bat`），bootstrap 使用基线工具链。
- 记录新工具链版本、`GOROOT` 和产物 hash。
- 基线工具链保留，不覆盖。
- 构建完成后运行补丁影响的标准库包官方单测和补丁自带单测；例如 `sync.Pool` 家族用 `"$BUILD_ROOT/bin/go" test sync -run 'TestPool' -count=1`。`-race` 仅用于功能验证，不用于性能测量。

### Step 3：服务重编译

- 用 `$BUILD_ROOT/bin/go` 清理缓存后重编译业务二进制。
- 记录 `go version -m`、产物 sha256、启动参数和健康检查。
- 除工具链外，编译参数、依赖、配置和绑核保持一致。
- 标准库单测通过后才允许进入服务级正确性和 A/B；单测证据写入 `correctness_evidence`。

### Step 4：二进制等价门控

- 记录业务源码 commit、构建参数、依赖版本、运行库、启动参数、环境变量、cpuset/NUMA 和配置差异。
- 若同时改变源码、配置、运行库或绑核，本轮标记为 `confounded_binary_test`，收益不得归因给补丁。

### Step 5：回退

- 恢复基线工具链、基线二进制和原环境变量，重启服务。
- 撤销补丁或删除 `BUILD_ROOT`，复核基线 `bin/go` 的版本和 sha256。
- `reverse_cmd` 必须包含：撤销补丁/删除构建根、恢复基线工具链、恢复基线二进制、重启、身份与健康复核。

## 分支、终止与交回

- 补丁与基线不匹配、补丁 hash/来源缺失：`status=blocked_patch_source`，交回基线版本、补丁 hash 和失败证据，不做模糊套用。
- `BUILD_ROOT` realpath/标记校验失败：`status=blocked_build_root`，交回实际路径和未执行清理的说明。
- 缺少重编译/替换/重启授权：`status=blocked_authorization`，交回待授权项，不执行构建或替换。
- 标准库单测或补丁自带单测失败：终止本轮，交回失败包、命令和日志位置，不进入服务重编译/A/B。
- 构建或单测超时、磁盘不足：终止本轮，交回阶段、耗时、磁盘余量和清理/恢复命令。
- 二进制等价门控出现源码/配置/运行库/绑核变化：标记 `confounded_binary_test`，仍可交回构建产物，但收益不得归因给补丁。
- 正常完成：交回 `toolchain_source`、`binary_equivalence_check`、`correctness_evidence`、`candidate_actions` 和新二进制身份。

## 输出字段

- `go_baseline_version`
- `toolchain_source`
- `patch_scope`
- `binary_equivalence_check`
- `confounded_binary_test`
- `correctness_evidence`
- `candidate_actions`

## 拒绝条件

- 无法记录 Go 基线、补丁 hash 或业务二进制身份。
- 不允许重编译工具链或业务二进制。
- 补丁与基线不匹配且无法重写。
- 目标二进制使用 `-race` 或 `-cover` 构建。

## 边界

- 不定义补丁行为、GODEBUG 键名、参数取值或业务优化语义；这些由调用方 skill 提供。
- 不保存补丁原文；补丁通过外部路径传入，仓库只保留通用流程。
- 基线 `$GOROOT` 只读；所有补丁和构建在隔离构建根目录完成。
