# Nginx 调优链（microservice 子域）

本子域承载 nginx 调优链。`install.sh` 只在含 `DOMAIN.md` 的目录递归安装；本文件使
`skills/microservice/nginx/<skill>/` 下的 skill 对 claude / opencode 两条安装链可见。
成员按系列 PR 合入；未合入成员的登记行为规划信息，目录在对应 PR 合入后出现。

## 成员

| Skill | 职责 |
|---|---|
| `nginx-baseline-setup` | 调优链入口：场景四要素、部署检查、L0 基线与调优循环纪律 |
| `nginx-bottleneck-analysis` | 瓶颈判定树、perf/PMU/网卡/TCP 证据采集与分级路由 |
| `nginx-env-tuning` | 环境级：干扰服务、句柄、网卡多队列/RSS、IRQ 绑核 |
| `nginx-conf-tuning` | 配置级：四场景 nginx.conf 决策、TLS/session/reuseport 模板 |
| `nginx-kunpeng-tuning` | 加速级：KAL/KAE/tcmalloc/xcall 与回退纪律 |

## 组内路由

默认从 `nginx-baseline-setup` 建立 L0 基线，再按瓶颈结论进入环境级 / 配置级 / 加速级；每级变更后回基线复测。路由以各 SKILL.md 与 `microservice/DOMAIN.md` 为准。
