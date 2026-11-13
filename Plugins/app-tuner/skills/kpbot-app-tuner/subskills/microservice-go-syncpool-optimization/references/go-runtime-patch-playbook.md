# Go Runtime 补丁与重编译 Playbook

本文件约束 `microservice-go-syncpool-optimization` 的补丁应用、工具链构建、服务重编译、二进制等价门控和回退。所有命令在目标节点执行，执行前确认已获得重编译授权。

## 1. 补丁来源与组成

本 skill 不保存、不生成补丁内容。补丁由内部私有制品提供，执行时通过 `--patch-file` 参数或 `KPBOT_SYNCPOOL_PATCH` 环境变量指定本地路径；路径只写入本轮执行记录，不写入仓库。

补丁包含四类行为，具体实现由补丁维护者负责：

| 行为 | 说明 |
|---|---|
| 开关注册 | 让 runtime 在进程启动时解析 `GODEBUG=poolretain` 与 `GODEBUG=poolprealloc` |
| 回收旁路 | `poolretain` 非 0 时，GC 阶段跳过 `sync.Pool` 清理，其它清理不受影响 |
| 创建预热 | `poolprealloc` 非 0 时，池的 per-P 本地缓存首次创建时预分配对象，并带内置安全上限 |
| 单元测试 | 覆盖预分配、上限、无 `New`、覆盖值和 Get/Put 复用 |

补丁文件不得提交到 KPBot 仓库。

## 2. Step 0：Go 基线记录

```bash
go version
cat "$GOROOT/VERSION"
git -C "$GOROOT" describe --tags --always
git -C "$GOROOT" status --short
sha256sum <go-src-tarball>
```

必须记录到轮次结果：Go tag/commit、源码包 hash、补丁文件 hash、`GOROOT` 路径。基线缺失时不进入构建。

## 3. 补丁与 Go 基线匹配校验

```bash
git -C "$GOROOT" apply --check <patch-file>
```

- 通过：继续应用。
- 失败：说明私有补丁与当前 Go 基线不匹配，停止本轮，交由补丁维护者重写；agent 不得手工拼接或模糊套用。
- 记录补丁文件 hash 与 `git apply --check` 结果。

## 4. 应用补丁

```bash
git -C "$GOROOT" apply --check <patch-file>
git -C "$GOROOT" apply <patch-file>
git -C "$GOROOT" diff --stat
```

记录 `git diff` 全文和补丁 hash。应用失败时停止，不做手工拼接。

## 5. 构建工具链

```bash
cd "$GOROOT/src"
GOROOT_BOOTSTRAP=<bootstrap-goroot> ./make.bash
"$GOROOT/bin/go" version
"$GOROOT/bin/go" env GOROOT
```

要求：

- bootstrap 工具链版本满足 Go 源码要求；
- 构建日志完整保存；
- 新工具链产物 hash 与版本字符串记录到结果；
- 原工具链目录保留，供回退使用。

Windows 环境使用 `make.bat`，流程相同。

## 6. 服务重编译

```bash
"$GOROOT/bin/go" clean -cache
"$GOROOT/bin/go" build -trimpath -buildvcs=false -o <new-binary> <main-package>
go version -m <new-binary>
sha256sum <new-binary>
```

要求：

- 重编译前后除工具链外，编译参数、链接库、启动参数和配置保持一致；
- 记录 `go version -m` 中的 toolchain、build settings 和依赖版本；
- 启动后复核 `/proc/<pid>/exe`、`cmdline`、`maps` 和健康检查。

## 7. 二进制等价门控

本轮只能改变 Go 工具链和 `sync.Pool` 补丁。以下项目必须逐项记录并保持一致：

- 业务源码 commit；
- 构建命令与编译参数；
- 依赖版本与 vendor 状态；
- 运行库、动态链接方式；
- 启动参数、环境变量、cpuset/NUMA 绑定；
- 配置文件和数据目录。

若同时改变源码、配置、运行库或绑核，本轮标记为 `confounded_binary_test`，收益不得归因给补丁。

## 8. 回退

```bash
export GOROOT=<baseline-goroot>
<restore baseline binary or rebuild with baseline GOROOT>
<restart service>
```

回退后必须复核二进制身份、`go version -m`、健康检查和基线指标。`reverse_cmd` 必须可独立执行，不依赖对话上下文。

## 9. 常见失败

- 补丁上下文不匹配：Go 版本与补丁基线不一致，重写补丁而不是模糊套用。
- `make.bash` 失败：bootstrap 版本不满足要求，或源码树被改动，检查构建日志。
- 新工具链能构建但服务启动失败：检查 cgo、动态库和 `GOROOT` 是否混用。
- 性能无可观测变化：确认新二进制确实由新工具链构建（`go version -m`），且 `GODEBUG` 已生效。
- 内存持续上涨：`poolretain` 无上限，检查池对象大小和调用方是否长时间不 `Put`。
