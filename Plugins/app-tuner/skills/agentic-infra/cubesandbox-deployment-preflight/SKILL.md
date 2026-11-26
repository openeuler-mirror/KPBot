---
name: cubesandbox-deployment-preflight
description: >
  对计划全新安装 CubeSandbox 单机 all-in-one 的 Linux 主机执行只读部署预检。
  适用于“CubeSandbox 部署前检查”“检查架构/Release/KVM/CIDR/端口是否满足安装条件”；
  输出机器可读 JSON 就绪结论，不执行安装、验收或主机修复。涉及多 IP、PVM、CIDR、
  代理或私有镜像时只报告需要用户确认的项目。不用于升级、恢复、离线、多机、模板或性能测试。
license: Proprietary
compatibility: Linux x86_64/aarch64; root for deployment-equivalent checks; online Release metadata access
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox 全新安装只读预检

唯一职责是回答“当前主机和配置是否可以进入**全新单机安装授权阶段**”。本 Skill 不安装、
不验收、不自动修复。Release 与 env 解析复用 `cubesandbox-deployment` 的内部工具。

## 输入

- 目标机为 Linux x86_64/aarch64；按正式部署身份运行才能得到等价权限结论。
- 可选的严格 env 文件，规则见[共享部署参考](../cubesandbox-deployment/references/deployment.md)。
- 一个可写报告父目录及非符号链接 JSON 报告路径。按真实路径解析后，报告不得与 `--env`
  输入重合；已有文件只能是可解析且 `schema_version=1`、`skill=cubesandbox-deployment-preflight`、
  `mode=preflight` 的同类旧报告，其他文件一律拒绝覆盖。
- 同级共享 Skill `cubesandbox-deployment`；源码域目录和安装后的平铺目录均按此布局解析。

## 可执行入口

```bash
bash scripts/run.sh --report /secure/path/cubesandbox-preflight.json
# 有用户确认的覆盖项时：
bash scripts/run.sh --env /secure/path/deployment.env \
  --report /secure/path/cubesandbox-preflight.json
```

入口固定调用共享 `../cubesandbox-deployment/scripts/deploy.sh --preflight-only`，不会把调用方
参数转成安装模式。允许访问 Release 元数据，但不下载发布包或镜像。报告安全性在主机检查和
Release 解析前先验证，写入使用同类身份复核和原子 `0600` 替换。

## 输出与判读

退出码：`0`=就绪，`1`=检查完成但不就绪，`2`=用法错误，`4`=blocked。报告权限为 `0600`，
主要字段包括：

- `status`: `success`、`failed` 或 `blocked`；
- `success` / `ready_for_install`: 只有两个都为 `true` 才能进入授权阶段；
- `release`、`host`: 已解析的版本/资产、Release 架构和 PVM 选择；
- `sha256_pinned`: 是否已取得并强制固定可信 SHA-256；必须为 `true` 才可能就绪；
- `issues`、`warnings`: 需要处理或由用户确认的事项；
- `planned_install_actions_not_performed`: 例如 bpffs/PVM 的 **AUTO 计划，未执行**；
- `side_effects`: 主机配置未改、未下载大包、未启动安装的明确记录。

所有安装来源都必须有有效 SHA-256。GitHub API 回退元数据或自定义 URL 没有可信摘要时，
报告为 `failed`、`ready_for_install=false`、`sha256_pinned=false` 并保留 `issues`；不得继续
主机准备、下载或安装。自定义 URL 也必须同时提供 `CUBE_SANDBOX_SHA256`。

检测到已有安装时，即使兼容版 `deploy.sh` 只给出 warning，本聚焦入口也会输出
`ready_for_install=false` 并返回 `1`。不得只凭人读日志或兼容入口退出码声称就绪。

## 成功、失败与 blocked

- **成功**：主机、配置、CIDR/端口、Release 与摘要固定全部完成，且 `success=true`、
  `ready_for_install=true`、`sha256_pinned=true`。这只是安装前结论，不是部署健康结论。
- **失败**：存在可修复的前置问题、已有/部分/未知安装，或 Release/env/摘要校验不通过；保留
  `issues`，不得绕过检查。
- **blocked**：共享实现缺失，或无法安全写出报告。补齐同版本的共享 Skill/报告位置后重试；
  不复制 Release/env 实现到本 Skill。危险报告路径不会被写入或用于覆盖旧文件。

## 无副作用边界与安装交接

除写指定 JSON 报告和临时日志外，不写 fstab、不挂载 bpffs、不加载模块、不改配置、不调用
systemctl、不启动安装器。预检后由 `cubesandbox-deployment` 汇总变更并取得明确授权，再把
已批准报告的 `release.version` 原样传给 `cubesandbox-single-node-install --expect-version`；
健康验收路由到 `cubesandbox-deployment-verify`。
