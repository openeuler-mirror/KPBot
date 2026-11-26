# 暂存、试启动与回退

## Host 或带自身引导器的 VM

1. 在实际目标记录运行 release、boot ID、默认启动项、已安装内核、当前根文件系统和管理网络所用驱动。盘点本次重启会影响的服务和活动任务。确认旧内核、模块与 initramfs 可用，保存启动配置和恢复入口。在共享节点沿用本次操作范围和维护窗口。
2. 先在工作盘暂存。核对 `/boot`、ESP、根文件系统和模块存储的可用空间。对 RPM/DEB 用本机的包查询工具核实包身份、匹配模块、安装脚本与旧内核保留策略。手工安装时为候选使用独立文件名和 `/lib/modules/<kernelrelease>`。实际根盘、文件系统、管理网卡所需驱动应已内建或在匹配的 initramfs 及其加载配置中可用。按目标的签名策略验证。
3. 探测引导方式、菜单项 ID 和实际加载的配置。Ubuntu 的 GRUB 流程可使用本机已有的 `update-initramfs`、`update-grub` 和 `grub-reboot`。openEuler 按本机的 dracut、GRUB2/BLS 或实际引导工具生成并枚举条目。安装新包或重建菜单可能改变“第一个条目”。在设置一次性目标前，按菜单 ID 核实下次常规启动仍指向原内核，提前解决默认项漂移。GRUB 的一次性选择依赖可写的环境块。遇到其他引导器，使用该后端已验证的选项和返回路径。操作前留好控制台、带外或现场恢复方式。[GRUB 一次性引导](https://manpages.ubuntu.com/manpages/noble/man8/grub-reboot.8.html)
4. 在控制端记录 `requested`、目标机器、先前 boot ID、候选身份、恢复项和预计返回时间。已确认使用 Ubuntu GRUB 菜单 ID 时，可用 `grub-reboot "$CANDIDATE_ENTRY"` 设置本次目标，用 `grub-editenv "$GRUB_ENV" list` 在实际环境块读回 `next_entry`。核对后发出一次重启请求。连接断开后等待既定启动窗口并重连。中断后先读取当前 boot ID、实际 release、机制和待引导条目。boot ID 未变时，检查上次请求和一次性条目是否仍在等待。boot ID 已变却已回到旧内核时，收集此次引导日志和回退原因。若目标未回来，使用已核实的恢复通道检查引导现场并选择旧项。
5. 在目标执行下面的检查。`EXPECTED_RELEASE` 来自已暂存产物，`OLD_BOOT_ID` 来自同一台机器切换前。需要区分同 release 的构建时，追加 `--build-id` 并使用候选未压缩 `vmlinux` 的 GNU build ID。无法取得可区分身份时保留为待核实。

   ```sh
   python3 scripts/check_kernel.py boot --release "$EXPECTED_RELEASE" \
     --run-id "$RUN_ID" --target-id "$TARGET_ID" \
     --previous-boot-id "$OLD_BOOT_ID" --expect "$REQUESTED_CONFIG" > "$RUN/boot-check.json"
   ```

   `--expect` 可省略或重复。`--has-arg` 和 `--lacks-arg` 核对传给内核的完整参数词，在分隔符 `--` 处停止。引导参数出现在 cmdline 后，还要读取实现提供的运行值、日志或计数。供离线核对时，`--root` 指向来自同一目标同一次启动的捕获目录，保持 `proc/sys/kernel/osrelease`、`proc/sys/kernel/random/boot_id` 等原相对路径。按请求补采 `proc/cmdline`、配置、`sys/kernel/notes`。核对配置时，结果中的 `config_source` 指明来源。`proc/config.gz` 为运行内核导出的配置。若只能取得磁盘上的 `boot/config-<release>`，用包或构建记录核对它与运行镜像的对应关系，尤其是存在同名 release 时。

6. 按目标核对根卷和数据卷挂载、管理网络、所需模块、设备权限及先前服务的可用性。记录新进程的二进制和服务身份，然后运行事先选定的业务操作。回退时依相同路径引导旧内核，重新核对 boot ID、原配置、默认项、外置模块、服务和业务操作，分别保存结果。

## 由 VMM 指定内核的 Guest

核实目标架构和 VMM 直接加载所需的镜像格式。发行版交付的 EFI 包装内核有时需要按发布格式解包。记录原件和最终交给 VMM 的文件摘要。

由启动方关联 Guest 镜像摘要、命令行、rootfs、VMM 版本及实例 ID。Firecracker 的冷启动用所选内核。AgentENV 可在独立测试服务配置中用 `kernel.image_path` 选镜像。其他 VMM 按其实际引导接口处理。分别在 Host 和该 Guest 内取证，按被变更的一层核验。[AgentENV 镜像选择](https://github.com/kvcache-ai/AgentENV/blob/0359440016183e0d77cade5832e5a30a944f9ec9/src/cfg.rs#L812)

替换 Guest 内核或启动参数后创建冷启动实例并在内部读取 release、构建身份和开关。需测恢复时由该状态制作匹配的内存快照。恢复旧内存快照会带回原来运行的 Guest 内核和状态。用相同 rootfs 比较两个内核时可共享只读基础镜像，为各组保留可区分的实例与可写层。回退用原镜像、原参数及匹配的快照，再读回业务标记。[Firecracker 快照内容](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)

## AgentENV 沙箱验收

将测试服务的 `[kernel].image_path` 指向候选文件，按该版本的配置加载方式使测试服务采用新路径。固定底层 OCI 镜像版本或摘要。下面的命令适用于支持 `aenv start --cold` 的版本，CLI 应已指向这套测试服务。旧版本可用其冷启动 API 创建沙箱并通过 envd 执行同样的检查。`RUN` 是本轮控制端记录目录，`EXPECTED_RELEASE` 来自候选产物。[AgentENV 冷启动接口](https://github.com/kvcache-ai/AgentENV/blob/23af8d99c95f704f7271fc11149410098f78f4a3/docs/src/concepts/sandboxes/starting.md#L55)

```sh
set -eu
sandbox=$(aenv start --cold "$TEST_IMAGE" --detach)
printf '%s\n' "$sandbox" > "$RUN/sandbox-id"
```

`--detach` 返回 ID 后，envd 可能仍在启动。后续步骤从记录中读取同一 ID。下面的只读探针使用本轮约定的就绪窗口，可通过 `READY_TIMEOUT_S` 调整示例的 90 秒。

```sh
set -eu
sandbox=$(cat "$RUN/sandbox-id")
wait_envd() {
    deadline=$(( $(date +%s) + ${READY_TIMEOUT_S:-90} ))
    until aenv exec "$sandbox" uname -r > "$1" 2>> "$RUN/envd-errors.log"; do
        [ "$(date +%s)" -lt "$deadline" ] || return 1
        sleep 1
    done
}
wait_envd "$RUN/guest-release"
test "$(cat "$RUN/guest-release")" = "$EXPECTED_RELEASE"
aenv exec "$sandbox" cat /proc/sys/kernel/random/boot_id > "$RUN/guest-boot-id"
test -s "$RUN/guest-boot-id"
```

若新旧 Guest 镜像的 release 相同，额外采集该 Guest 的 `/sys/kernel/notes`，用 `check_kernel.py boot --build-id` 核对候选 `vmlinux` 的 GNU build ID。需要验收暂停恢复时，在同一控制端 shell 继续操作刚创建的沙箱。用冷启动时的 boot ID 当作文件内容。恢复后复核内核，并读回同一文件与控制端保留的值比较。

```sh
marker=$(aenv exec "$sandbox" mktemp /tmp/kpbot-kernel.XXXXXX)
aenv exec "$sandbox" cp /proc/sys/kernel/random/boot_id "$marker"
aenv pause "$sandbox"
aenv resume "$sandbox"
wait_envd "$RUN/resumed-release"
cmp "$RUN/guest-release" "$RUN/resumed-release"
aenv exec "$sandbox" cat "$marker" > "$RUN/marker-after-resume"
cmp "$RUN/guest-boot-id" "$RUN/marker-after-resume"
```

核对本轮要求的机制和业务操作后，用 `aenv delete "$sandbox"` 清理本轮创建的沙箱。验收失败时先留存该实例的串口、envd 日志及失败步骤。回退验证用原镜像另建沙箱。判断一次新的冷启动时可以比较 boot ID，同一快照的 resume 则检查已捕获状态能否延续。
