---
name: cubesandbox-deployment
description: >
  CubeSandbox 单机在线部署的一键 Workflow 与共享实现路由。适用于“安装/部署
  CubeSandbox 单机 all-in-one”，编排只读预检、用户授权、全新安装和独立健康验收；
  兼容原 deploy.sh，集中维护 amd64/arm64 Release、严格 env、SHA-256、官方安装器及
  quickcheck。升级/恢复/离线/多机/PVM 内核自动安装、模板与性能测试不在范围内。
license: Proprietary
compatibility: Linux x86_64/aarch64; root; systemd; native KVM or prepared x86_64 PVM; online access
metadata:
  skill_type: workflow
  category: deployment
  domain: cubesandbox
  version: "1.1.0"
---

# CubeSandbox 单机部署 Workflow

这是部署路由和共享实现入口，不是第四份聚焦实现。三个独立 Skill 分别承担预检、授权安装、
只读验收；Release 解析和 env 解析仍是本目录内部工具，不单独技能化。

## 路由与共享架构

| 阶段 | 聚焦 Skill | 真实入口 |
|---|---|---|
| 只读预检 | `cubesandbox-deployment-preflight` | `../cubesandbox-deployment-preflight/scripts/run.sh` |
| 明确授权后的全新安装 | `cubesandbox-single-node-install` | `../cubesandbox-single-node-install/scripts/run.sh` |
| 独立只读验收 | `cubesandbox-deployment-verify` | `../cubesandbox-deployment-verify/scripts/run.sh` |

三个入口均以同级 `cubesandbox-deployment` 为共享库；相对路径在源码 `agentic-infra/` 和
安装后的平铺 `skills/` 中一致。缺共享库必须 blocked，不能复制 `deploy.sh`、
`discover_release.py`、`parse_env.py` 或 references 来绕过依赖。

- `scripts/deploy.sh`：兼容的一键预检/安装入口，以及唯一的主机准备、Release 下载校验、
  安全门、官方 `install.sh` 和安装期官方 quickcheck 实现；
- `scripts/verify.sh`：唯一的独立验收实现，只执行已部署官方 quickcheck 并输出 JSON；
- `scripts/report_safety.py`：预检/验收共用的真实路径、防覆盖与原子 `0600` 报告实现；
- `scripts/discover_release.py`、`scripts/parse_env.py`：内部 Release/env 工具；
- `references/deployment.md`：共享变量、副作用、安全和故障处理参考。
- `cases/deployment-contract/`：共享部署实现的静态、严格 env 与 Release 契约，覆盖安全门、强制摘要和双架构资产选择；在本 Skill 目录运行 `bash cases/deployment-contract/run.sh`。

## 标准 Workflow

### 1. 只读预检

在目标机运行：

```bash
bash ../cubesandbox-deployment-preflight/scripts/run.sh \
  --report /secure/path/cubesandbox-preflight.json
# 如需覆盖项，增加 --env /secure/path/deployment.env
```

只有报告 `status=success`、`success=true`、`ready_for_install=true`、`sha256_pinned=true` 才能继续。
报告不得覆盖 `--env` 或任意非同类旧文件；已有同类 JSON 也只能经身份复核后原子 `0600` 替换。
所有 Release 来源都必须有可信 SHA-256，API 回退和自定义 URL 缺摘要时预检不就绪。`AUTO` 仅是
安装期计划；此阶段不挂载 bpffs、不加载模块、不写 fstab、不下载大包、不启动安装。

### 2. 汇总变更并取得明确授权

按[共享参考](references/deployment.md)只询问必要变量，说明版本/架构、节点 IP、KVM/PVM、
CN/全球路线、固定安装根以及 Docker、systemd、DNS、iptables、`/data/cubelet` 等变更。
多 IP、端口/CIDR 冲突、已有/部分/未知安装或破坏性前置操作未确认时停止。

### 3. 执行全新安装

```bash
EXPECTED_VERSION="$(python3 -c 'import json, sys; r = json.load(open(sys.argv[1], encoding="utf-8")); assert r["ready_for_install"] is True and r["sha256_pinned"] is True; print(r["release"]["version"])' \
  /secure/path/cubesandbox-preflight.json)"
bash ../cubesandbox-single-node-install/scripts/run.sh \
  --confirm-fresh-install \
  --expect-version "${EXPECTED_VERSION}"
# 使用预检时的同一份配置时，追加 --env /secure/path/deployment.env
```

`--expect-version` 必须原样来自已批准报告的 `release.version`。安装入口会重新预检并核对实际
`RESOLVED_VERSION`；若 `latest` 已漂移，则主机准备、下载和安装均未发生即停止，必须重新预检和
授权。该入口不能转为 preflight/verify/upgrade，也不能关闭安全门。所有资产强制 SHA-256；官方
安装器及其 quickcheck 成功且 `.one-click.env` 存在才是安装成功；失败不自动清理、升级或恢复。
Agent/包装器不得读取、打印或复制 `.one-click.env` 的秘密内容。
成功报告还需提醒：默认 CubeAPI 为 `127.0.0.1:3000`、WebUI 为 `<node-ip>:12088`；API/WebUI 不直接暴露到不可信网络。

### 4. 独立只读验收

```bash
bash ../cubesandbox-deployment-verify/scripts/run.sh \
  --report /secure/path/cubesandbox-verify.json
```

缺官方 quickcheck、`.one-click.env`、完整官方证据或官方 runtime/log 目录时必须 blocked；健康检查
非零即失败。报告不得落在安装根或覆盖非同类的已有文件。官方 v0.7.3 的
`ONE_CLICK_RUNTIME_DIR`、`ONE_CLICK_LOG_DIR` 必须预先存在，验收绝不创建；转发日志会移除所有
HTTP(S) URL 的用户信息、路径和 query。不能用进程曾启动或少数 superficial 检查冒充验收成功。

## 原有一键用法兼容

希望由单个命令完成“预检 → 安装 → 安装期官方 quickcheck”时，原有调用不变：

```bash
bash scripts/deploy.sh --preflight-only                  # 仅预检
bash scripts/deploy.sh --env /secure/path/deployment.env # 授权后安装
bash scripts/deploy.sh                                   # 无 env 覆盖时
```

`deploy.sh` 仍在安装前再次执行完整预检，且安装期官方 quickcheck 不可关闭；每次调用只解析一次
Release 并强制可信 SHA-256。需要独立复核或机器可读验收报告时，再运行第 4 步；它不会触发
Release 发现、下载、安装、重启、目录创建或配置修改。

## 不可违反的边界

1. 只支持全新单机 all-in-one；完整、部分或无法识别的已有安装均拒绝。升级/恢复转用户明确
   授权的官方手工流程。
2. 不使用 `ONE_CLICK_SKIP_PRECHECK=1`、`ONE_CLICK_SKIP_PVM_CHECK=1`，不关闭 quickcheck，
   不通过自定义根目录、角色或本地包绕过保护。
3. 不自动格式化磁盘、安装 PVM 宿主内核、修改 GRUB 或重启；这些必须另行授权。
4. 不创建模板或真实沙箱，不运行 cube-bench、Pause/Resume、E2B 示例或性能测试；不新增离线、
   多机能力。
5. 密码、Token、私钥和带凭据代理 URL 不写入 Skill、仓库、日志或 shell 历史。env 文件放在
   Skill 外并限制权限，报告只保留非秘密结论。

成功报告需区分“预检就绪”“安装完成”“独立验收健康”。任何阶段失败或 blocked 都不能
宣称一键部署成功。变量细节、架构差异和排障见[共享部署参考](references/deployment.md)。
