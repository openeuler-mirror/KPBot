---
name: cubesandbox-bench-preflight
description: >
  当需要在 CubeSandbox 基准前只读检查服务、API、模板、资源独占、NUMA、TAP、SDK 和所选官方工具时使用。只执行 check 并写 preflight.json；不准备、不冒烟、不压测，也不因用户要求而自动修复环境。
license: Apache-2.0
compatibility: Linux；已部署 CubeSandbox；共享 cubesandbox-lifecycle-bench 执行器
metadata:
  skill_type: tool_assisted
  category: benchmark
  domain: cubesandbox
  version: "1.0.0"
---

# CubeSandbox Bench 只读预检

## 唯一职责与输入

只调用共享执行器的 `check`：采集服务/API/模板、沙箱与 shim 残留、负载、内存磁盘、NUMA/TAP 和 SDK 证据。
输入为 `CUBE_ROOT`、API、可选 Template、NUMA/TAP 目标及计划；默认检查完整计划，可用 `--sections`
限定即将执行的章节。代理变量只在子进程环境中移除，不修改用户 shell。

## 可执行入口

```bash
SKILL_DIR=/path/to/cubesandbox-bench-preflight
CUBE_ROOT=/path/to/CubeSandbox
bash "$SKILL_DIR/scripts/run.sh" \
  --repo "$CUBE_ROOT" --template-id tpl-xxx \
  --api-url http://127.0.0.1:3000 --numa-node 0 --tap-target 500 \
  --output /path/preflight.json
```

省略 `--output` 时写当前目录 `preflight.json`，stdout 同步输出同一 JSON。准备前从
`context.tap_evidence_sha256` 读取精确 TAP 集合证据；多个 READY 模板时必须由用户选择。

## 输出与成功判据

退出 0 且 `preflight.json.ok=true`、没有 FAIL 才表示只读预检通过；WARN 必须随结果交付。
`context` 和 `checks[]` 是后续 prepare/run 的输入证据。输出父目录按可信目录处理；最终报告若是符号链接
会被拒绝，不会写链接目标。报告使用独占创建、权限 0600 的唯一临时文件，写完后以 `os.replace` 原子替换
目录项，因此并发预检不共用 `.tmp`，替换竞态也不会跟随最终符号链接。除写该报告外不安装依赖、不改启动
脚本、不删接口、不重启、不创建沙箱。

## 受阻与边界

任一 FAIL 返回 1，仍保留 `ok=false` 的报告供诊断；参数错误返回 2。缺少 `cubemastercli` 时记录
`template.ready=FAIL`，写出本次 `ok=false` 报告覆盖同路径旧结果并返回 1，不以 traceback 中断或沿用旧的
通过报告。服务/API 结构未知、分页不完整、模板不 READY、资源不独占、NUMA/TAP 不符都不能被解释成
“基本可用”，本 Skill 不会自动 prepare 或开测。
详细口径见 [共享预检说明](../cubesandbox-lifecycle-bench/references/preflight-and-prepare.md)。

## 自检

```bash
bash "$SKILL_DIR/cases/skill-contract/run.sh"
```

自检以 mock 验证成功/失败报告及误用退出，不读取真实宿主机配置。
