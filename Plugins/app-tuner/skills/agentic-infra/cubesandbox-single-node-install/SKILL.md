---
name: cubesandbox-single-node-install
description: >
  在用户明确授权后，全新安装单机 CubeSandbox（Cube Sandbox）all-in-one 环境。
  适用于已确认变更摘要的“安装 CubeSandbox 单机版”任务；强制复用共享安全预检、
  amd64/arm64 Release 选择、SHA-256、官方 install.sh 和官方健康 quickcheck。
  不执行独立预检或验收模式，不支持已有/部分/未知安装、升级、恢复、离线、多机、
  PVM 内核自动安装、模板、沙箱业务或性能测试。
license: Proprietary
compatibility: Linux x86_64/aarch64; root; systemd; native KVM or prepared x86_64 PVM; online access
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox 全新单机安装

唯一职责是在明确授权后执行一次**全新、在线、单机 all-in-one 安装**。本入口不是通用参数
转发器；主机准备与官方安装实现只保留在 `cubesandbox-deployment/scripts/deploy.sh`。

## 输入与授权门

1. 先由 `cubesandbox-deployment-preflight` 得到 `ready_for_install=true` 且
   `sha256_pinned=true` 的报告；向用户说明版本/架构、节点 IP、KVM/PVM、镜像路线、安装根目录
   和系统变更。
2. 用户明确允许官方安装器修改 Docker、systemd、DNS、iptables、`/data/cubelet` 和
   `/usr/local/services/cubetoolbox` 后，才使用 `--confirm-fresh-install`。
3. 把**同一份已批准报告**的 `release.version` 原样传给必需的 `--expect-version`。安装时再次
   解析到不同版本会立即停止，必须重新预检并重新取得授权，不能安装漂移后的 `latest`。
4. 可选 env 文件必须位于 Skill/仓库外并限制权限；秘密不能进入命令行、报告或聊天。
5. 同级共享 Skill `cubesandbox-deployment` 必须存在。

## 可执行入口

```bash
PREFLIGHT_REPORT=/secure/path/cubesandbox-preflight.json
EXPECTED_VERSION="$(python3 - "${PREFLIGHT_REPORT}" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
assert report["ready_for_install"] is True and report["sha256_pinned"] is True
print(report["release"]["version"])
PY
)"
bash scripts/run.sh --confirm-fresh-install \
  --expect-version "${EXPECTED_VERSION}"
# 使用预检时的同一份 env 时，追加：
#   --env /secure/path/deployment.env
```

入口只接受授权开关、精确版本和 env 文件，并固定调用共享 `deploy.sh` 的默认全新安装路径。
`--preflight-only`、`--verify-only`、`--upgrade`、未知参数以及预检/PVM/quickcheck 绕过开关
均在安装前拒绝；不得通过修改角色、安装根目录或本地包参数规避安全门。

## 保留的安全保护

共享实现继续负责：完整/部分/未知已有安装拒绝，多 IP 显式确认，CIDR 与端口检查，部署锁，
主机准备前和官方安装前二次检查，bpffs/PVM 边界，官方架构资产选择与**强制 SHA-256**校验，
`--expect-version` 与实际 `RESOLVED_VERSION` 核对，安全 curl 配置、严格 env 解析与秘密脱敏。
API 回退或自定义 URL 缺少可信摘要时 fail closed；版本或摘要不一致都在主机准备、下载和
安装前停止。官方 `install.sh` 始终启用服务健康 quickcheck。PVM 宿主内核安装、格式化磁盘、
修改 GRUB 和重启仍需独立授权，不能由本 Skill 自动执行。

## 输出、成功与 blocked

- 输出共享安装器的阶段日志、最终版本/架构/PVM/访问地址，并只确认运行时
  `/usr/local/services/cubetoolbox/.one-click.env` **是否存在**。
- 成功报告还需提醒：默认 CubeAPI 为 `127.0.0.1:3000`、WebUI 为 `<node-ip>:12088`；
  API/WebUI 不直接暴露到不可信网络。自定义端口时报告实际地址，但不得省略此提醒。
- Agent 和包装器不得读取、打印或复制 `.one-click.env` 的秘密内容；日志及任何报告只保留
  “存在/缺失”等非秘密结论，不输出配置值。共享包装器自身生成的下载来源日志不输出 URL
  凭据、路径或查询串。
- **成功**仅在官方安装器返回成功、其官方 quickcheck 返回成功且 `.one-click.env` 存在时成立。
  进程曾启动或包已下载不算成功。
- **失败**时保留原始错误，不盲目重试或清理现场；已有/部分/未知安装转用户授权的官方
  升级/恢复手册，本入口没有这些能力。
- **blocked**（退出 `4`）表示共享实现缺失，不能安全开始。用法/绕过拒绝返回 `2`，其他
  预检、版本漂移、下载、校验、安装或 quickcheck 失败均为非零，不能报告成功。

安装后可独立调用 `cubesandbox-deployment-verify` 复核当前健康；它不能替代本安装流程中
必须执行的官方 quickcheck。
