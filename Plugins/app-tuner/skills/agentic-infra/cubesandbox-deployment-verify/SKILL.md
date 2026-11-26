---
name: cubesandbox-deployment-verify
description: >
  对已安装的 CubeSandbox 单机 all-in-one 节点执行独立、只读健康验收。
  适用于“验收 CubeSandbox 部署”“复核官方 quickcheck/服务/健康端点”；
  必须确认 .one-click.env 并交给已部署的官方 quickcheck 使用，输出结构化结果。
  不执行 Release 发现、下载、安装、升级、恢复、重启、目录创建或配置修改；
  缺官方 quickcheck、运行目录或证据不完整时 blocked，不以 superficial 检查冒充成功。
license: Proprietary
compatibility: Linux; root; existing single-node all-in-one CubeSandbox installation
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox 独立只读健康验收

唯一职责是验收**当前已安装**的单机 all-in-one 节点。验收与安装分开，可在安装后或再次
排障时运行；不根据“进程曾启动”推断健康。

## 输入

- 固定官方安装根 `/usr/local/services/cubetoolbox`；
- 可读、普通非符号链接的 `.one-click.env`；
- 固定安装根下的 `/usr/local/services/cubetoolbox/scripts/one-click/quickcheck.sh` 及同目录 `common.sh`；
- 官方 v0.7.3 `common.sh` 使用的两个目录必须已存在且为目录：
  `ONE_CLICK_RUNTIME_DIR`（默认 `/var/run/cube-sandbox-one-click`）和
  `ONE_CLICK_LOG_DIR`（默认 `/var/log/cube-sandbox-one-click`）；
- 同级共享 Skill `cubesandbox-deployment` 和一个安全 JSON 报告路径。

本入口没有 env、安装根或模式覆盖参数。包装器只确认运行配置存在并解析上述两个非秘密目录键，
不 source、不读取或打印秘密配置值，也不复制其内容到报告。官方 quickcheck 通过显式
`ONE_CLICK_RUNTIME_ENV_FILE` 使用同一份配置，维护会话中的同名错误覆盖不会生效。

报告按真实路径检查，不得与运行配置重合或落在 `INSTALL_ROOT`/任何安装资产内。已有文件只能是
可解析且 `schema_version=1`、`skill=cubesandbox-deployment-verify`、`mode=verify` 的同类旧报告；
其他文件和符号链接均拒绝覆盖。写入采用原子 `0600` 替换。

## 可执行入口

```bash
bash scripts/run.sh --report /secure/path/cubesandbox-verify.json
```

入口转到共享 `../cubesandbox-deployment/scripts/verify.sh`，实际执行安装根内的官方
quickcheck。官方检查覆盖 systemd 单元、依赖容器、CubeAPI/CubeOps 等健康端点、socket 和
运行文件。包装器还要求官方输出包含 control 角色、systemd、CubeAPI、CubeOps 和
`[quickcheck] OK` 证据，防止空脚本或提前退出被误判为完整验收。

官方 `common.sh` 启动时会为 runtime/log 目录执行 `mkdir -p`，因此包装器必须在启动 quickcheck
前检查二者；缺任一项即写 `blocked` 报告并返回 `4`，绝不补建目录。转发日志前，所有 HTTP(S)
URL 的用户信息、路径和 query 都会移除，避免 `CUBELET_EGRESS_DUMP_URL` 的签名进入 stderr。

## 输出与成功判据

退出码：`0`=健康，`1`=官方健康检查失败，`2`=用法错误，`4`=blocked。JSON 只包含非秘密结论：

- `status` / `success` / `quickcheck_exit_code`；
- `checks.runtime_env`、`checks.official_quickcheck`、
  `checks.services_and_health_endpoints`；
- `official_evidence` 中各项官方输出锚点；
- 未执行 Release 发现、下载、安装/重启或配置修改的 `side_effects` 记录。

只有 `.one-click.env` 可读、目录齐全、官方 quickcheck 返回 `0` 且全部所需证据存在时才输出
`status=success`、`success=true`。不以一两个 systemctl/curl 探测替代官方完整检查。

## 失败与 blocked

- **失败**：官方 quickcheck 已运行但返回非零；报告服务/健康未通过，保留脱敏官方错误供排障。
- **blocked**：`.one-click.env`、官方 quickcheck/common、runtime/log 目录或共享入口缺失/不可读，
  权限不足，报告无法安全写出，或 quickcheck 虽返回零但官方 all-in-one 证据不完整。即使共享
  `verify.sh` 或整个共享 Skill 缺失，只要报告路径安全，也会把旧报告原子替换为本次
  `status=blocked`、`success=false` 后返回 `4`；无法安全写时明确失败且不覆盖哨兵文件。
- 本 Skill 不修复失败、不重启服务、不创建目录。升级/恢复/离线/多机/PVM 内核安装、模板与
  性能测试均越界。
