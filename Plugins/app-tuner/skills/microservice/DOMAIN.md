# 微服务域（app-tuner 侧）

线上服务进程的性能优化，本侧负责**改配置、改 OS 参数、换加速库**这一类不碰业务代码的动作。

与 `code-optimizer` 侧的同名域分工：那边负责**改实现**的热路径优化（pprof 定位后的代码改写、DPDK 数据面代码），
本侧负责**分层调优配置**（L1 环境 / L2 配置 / L3 加速库）。两侧 `DOMAIN.md` 互相指路。

## 成员

| Skill | 职责 |
|---|---|
| [`nginx-optimization`](nginx-optimization/SKILL.md) | Nginx 场景专属调优：http/https × 长/短连接四场景，L1 环境层（IRQ 绑核、多队列）、L2 配置层（worker/连接/缓冲）、L3 鲲鹏加速库（KAE 等）分层推进 |

本域后续迁入（逐个独立 PR）：

- `kptools-dpdk-tuning`（DPDK 应用诊断 + 顾问双模式：配置与运行参数侧调优）

## 域内路由

- 服务是 Go 写的、要定位热点 → `code-optimizer` 侧 `microservice/` 的 `go-pprof-analysis`
- 数据面转发路径要改代码 → `code-optimizer` 侧 `dpdk-optimization`
- 转发/Web 服务只调配置与系统参数 → 本侧 `nginx-optimization` / `kptools-dpdk-tuning`

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/microservice/**`
