# 从新源码或新补丁得到可并存的内核

## 取得准确的输入

从项目提供的仓库、发布归档或发行版源码包取得基线。记录来源、提交或包的完整版本与架构，按发布方提供的签名或摘要核验。补丁记录适用基线、依赖、顺序和文件摘要。有些下载地址会返回登录页，先确认内容可解析为 diff 或邮箱文件。涉及内核、VMM 和驱动的多个项目，各自使用自己的基线。

Git 路径可直接使用随包脚本。以下命令从技能目录运行，`RUN` 是留存基线时创建的本轮绝对目录，`$RUN/input` 由脚本独占创建。两个摘要取自已核验的发布记录。使用未修改的 tag 时省略补丁和摘要参数。

```sh
python3 scripts/prepare_source.py --repo "$KERNEL_REPO" --base "$KERNEL_BASE" \
  --run "$RUN/input" --patch "$FIRST_PATCH" --patch "$SECOND_PATCH" \
  --patch-sha256 "$FIRST_SHA256" --patch-sha256 "$SECOND_SHA256"
```

脚本在临时 Git 索引中按顺序应用统一格式 diff、单封补丁邮件或整个邮箱。支持 `git format-patch` 以及 lore 导出的 mboxrd。会跳过带 `[PATCH 0/N]` 编号的纯封面，并要求系列里有实际补丁。讨论邮件放在系列之外。成功后 `$RUN/input/source` 是 detached worktree，变更已暂存，`input/source.json` 中的 `patched_tree` 标识应用后的整棵树。给定摘要时在应用该文件前核对，摘要不符、冲突或无效输入返回非零，保留失败记录。使用已信任的本地仓库与完整对象做准备，构建前审查新增的脚本和依赖，在非特权构建环境中运行。先核对基线、上游是否已合入、系列顺序和缺失依赖。需要移植时，在新的候选副本上审查并生成新差异，再走同一验证。需要保留作者与提交历史时，可在独立 worktree 上走原生 `git am`。[Git 补丁应用](https://git-scm.com/docs/git-apply)

对 tarball 或 source RPM，先在新目录校验、解包。发行版 spec 自带补丁时优先使用该版本的准备阶段，检查实际展开的树和补丁顺序。需要复用脚本时，可把已经展开的受控源码导入临时 Git 仓库，记录原始归档摘要和导入提交。已有二进制包和 Guest 镜像可直接走暂存与启动核验。

直接用 `$RUN/input/source` 编译会读到已应用的改动。`HEAD` 仍指向基线提交。如果打包工具按提交导出源码或重新解压 tarball，先把候选树提交在独立分支，或将系列加入发行版的补丁清单，再核对实际送入编译器的源文件。

## 选择配置、工具链与构建目标

从 Host 发行版或已验证的 Guest 配置出发。同一份源码可以构建多个候选，给每个候选取一个 `CANDIDATE` 名称，例如 `host-hz250` 或 `guest-net`，各自保存配置、对象、release 和模块。以下 Bash 示例在 Linux 构建节点执行。`BASE_CONFIG`、`REQUESTED_CONFIG` 为绝对路径，`KARCH` 为内核架构名，`IMAGE_TARGET` 取自该树的 `make help` 或平台文档。跨架构时，所有 make 调用使用同一套 `CROSS_COMPILE` 或 `LLVM` 工具链。

```bash
set -eu
src="$RUN/input/source"
case "${CANDIDATE:-}" in ''|.|..|*[!a-zA-Z0-9._-]*) exit 2 ;; esac
case "${JOBS:-}" in ''|*[!0-9]*) exit 2 ;; esac
test "$JOBS" -gt 0
candidate="$RUN/candidates/$CANDIDATE"
out="$candidate/build"
mkdir -p "$RUN/candidates"
mkdir -m 700 "$candidate"
mkdir "$out"
export KCONFIG_CONFIG="$out/.config"
( cd "$out" && "$src/scripts/kconfig/merge_config.sh" -m "$BASE_CONFIG" "$REQUESTED_CONFIG" )
make -C "$src" O="$out" ARCH="$KARCH" olddefconfig
python3 scripts/check_kernel.py config --config "$out/.config" --expect "$REQUESTED_CONFIG" \
  > "$candidate/config-check.json"
make -s --no-print-directory -C "$src" O="$out" ARCH="$KARCH" kernelrelease > "$candidate/kernelrelease"
```

在同一 shell 中选择构建目标。Host 或需要可加载模块的 Guest 同时构建镜像和模块，并把模块暂存在本候选目录。

```bash
make -C "$src" O="$out" ARCH="$KARCH" -j"$JOBS" "$IMAGE_TARGET" modules
make -C "$src" O="$out" ARCH="$KARCH" INSTALL_MOD_PATH="$candidate/stage" modules_install
test -d "$candidate/stage/lib/modules/$(cat "$candidate/kernelrelease")"
```

AgentENV Guest 的根盘、文件系统及早期网络驱动都已内建且只需交付镜像时，可以只编译镜像目标。

```bash
make -C "$src" O="$out" ARCH="$KARCH" -j"$JOBS" "$IMAGE_TARGET"
```

将候选独立的 `CONFIG_LOCALVERSION` 写入配置片段，核对最后计算出的 release，记录实际产生的镜像、配置、模块和匹配 initramfs 的摘要。`olddefconfig` 可能因依赖或互斥 choice 改写输入，构建前比较最终配置与基线。`check_kernel.py` 对缺失符号或值偏离请求会返回非零。单项实验保留目标和必要派生差异，说明其他差异的影响。[Linux 内核构建说明](https://docs.kernel.org/admin-guide/README.html)

- **发行版包**沿用该发行版的 spec、debian/rules 或文档中的构建入口，检查 flavor、依赖、签名及包文件清单。包脚本生成的 initramfs、启动项与默认项可能随发行版变化，安装后逐项读回。
- **独立模块**按目标 release 准备匹配的内核构建树、模块 ABI 和签名条件。启用 `CONFIG_MODVERSIONS` 时需取得完整内核构建产出的 `Module.symvers`，再编译独立模块。记录磁盘模块的 `vermagic` 和摘要，核对实际加载模块可提供的 `/sys/module/<name>/srcversion`、参数或驱动日志，再做设备功能检查。[Kbuild 外部模块说明](https://docs.kernel.org/kbuild/modules.html)
- **多个候选**共用源码对象即可，每个独立配置使用独立的 `O=` 目录和 staging。遇到厂商驱动或证书构建错误，先核实目标实际依赖，再调整候选配置并记录 diff。根盘、远程管理网卡、虚拟化设备及其驱动按目标核实。
