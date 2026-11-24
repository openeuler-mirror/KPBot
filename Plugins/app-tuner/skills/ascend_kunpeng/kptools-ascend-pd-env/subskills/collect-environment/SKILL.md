---
name: collect-environment
description: >
  只读采集用户指定的 vLLM-Ascend 容器，按 P、D 分别保存环境快照。
  适用于单侧存档、升级前后留档、跨机器收集；不做环境对比或性能归因。
---

# P/D 环境采集

## 工作流程

1. 确认每个容器所在节点、容器名和角色（`P` 或 `D`）。只采用户指定的对象；
   角色不明确时先确认，不按容器名猜。跨机器时各自在容器所在的宿主机执行。
2. 在指定宿主机上确认本地 Docker Unix socket 和它报告的节点名。默认 socket
   通常为 `/var/run/docker.sock`；rootless 用管理员确认的本地 socket。
   无法确认归属时暂停，不沿用当前 context、远端地址或转发 socket。
3. 在父 skill 目录按角色调用下列命令。每个容器用单独的空输出目录。脚本固定
   一个 socket，核对 Docker 节点，再在同一端点上自检、核对容器名与完整 ID，
   最后用 ID 采集；`.tgz`、`.tar`、`.tar.gz` 结尾的容器名也不会被误当成包。

```bash
bash subskills/collect-environment/scripts/collect_role.sh <本地socket> <Docker节点名> <P容器名> P <P输出目录>
bash subskills/collect-environment/scripts/collect_role.sh <本地socket> <Docker节点名> <D容器名> D <D输出目录>
```

`--role` 只写整包标签，所以同一台机器上的 P、D 也要分两次采。用户提供容器内的
Python 绝对路径时，可作为最后一个参数传入。

## 输出

- 每侧分别记录节点、容器名、角色、采集时间和实际产物路径。可交给后续流程的
  快照是 `采集/` 目录或 `采集.tar.gz`；`采集/环境报告.html` 用于快速查看。
- 用脚本输出记录容器名和 ID，检查 `采集/collection_meta.json` 的 `targets`、`role`，并读
  `采集/SUMMARY.txt` 区分成功、跳过和失败。退出码为 0 不代表所有项都采齐。
- 分侧报告完成、部分完成、中断或失败。失败或中断的一侧列出原因、已保留产物和缺采项，
  不因另一侧完成就把整次任务写为完成。

采集沿用父 skill 的只读约束。采集包可能包含完整环境变量和启动参数，答复只引用
受限产物路径。
