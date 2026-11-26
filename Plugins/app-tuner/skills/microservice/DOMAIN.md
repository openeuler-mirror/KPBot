# 微服务域（app-tuner 侧）

线上服务进程的性能优化，本侧负责**改配置、改 OS 参数、换加速库、改运行时参数**这一类不碰业务代码的动作。

本域不再使用 `subskills/`。通用能力直接平铺在 `microservice/` 下；语言/runtime 和组件专属能力放到各自的专题目录，专题目录下再平铺 skill。

与 `code-optimizer` 侧的同名域分工：那边负责**改实现**的热路径优化（pprof 定位后的代码改写、DPDK 数据面代码），本侧负责**分层调优配置**（L1 环境 / L2 配置 / L3 加速库）。两侧 `DOMAIN.md` 互相指路。

## 成员

| Skill / 专题 | 职责 |
|---|---|
| [`microservice-ab-testing`](microservice-ab-testing/SKILL.md) | 跨语言微服务单变量 A/B 方法：固定负载、预热、指标、正确性、判定和回退 |
| [`go/`](go/DOMAIN.md) | Go 语言/runtime 专题：`go-runtime-patch-build`、`go-syncpool-optimization` |
| [`sonic-go/`](sonic-go/DOMAIN.md) | Sonic JSON 编解码组件专题：`sonic-go-pool-optimization` |
| [`nginx-baseline-setup`](nginx/nginx-baseline-setup/SKILL.md) | Nginx 调优链入口（部署基线）：场景四要素判定、标准化部署检查、压测基线建立（httpress/wrk/ab）与调优循环纪律（单变量、逐层归因） |
| [`nginx-bottleneck-analysis`](nginx/nginx-bottleneck-analysis/SKILL.md) | Nginx 瓶颈分析：7 类瓶颈判定树、SSL 握手排查优先级、perf 火焰图/PMU 证据采集、参数精调搜索空间，输出分级路由建议 |
| [`nginx-env-tuning`](nginx/nginx-env-tuning/SKILL.md) | Nginx 优化·环境级：干扰服务（存档/恢复）、文件句柄、网卡多队列/RSS、IRQ 逐队列绑核、worker×IRQ 布局 |
| [`nginx-conf-tuning`](nginx/nginx-conf-tuning/SKILL.md) | Nginx 优化·配置级：四场景 nginx.conf 决策表（worker/keepalive/ssl session/reuseport）、TLS 证书协议决策、模板与生效纪律 |
| [`nginx-kunpeng-tuning`](nginx/nginx-kunpeng-tuning/SKILL.md) | Nginx 优化·加速级：KAL OpenSSL（运行期注入）、KAE/UADK、tcmalloc、xcall，前置快照与 Safety Gates（A/B 验证后才保留） |

本域后续迁入（逐个独立 PR）：

- `kptools-dpdk-tuning`（DPDK 应用诊断 + 顾问双模式：配置与运行参数侧调优）

## 域内路由

- 任意组件需要通用 A/B 验证 → `microservice-ab-testing`
- Go 服务 `sync.Pool`/GC 颠簸 → `go/go-syncpool-optimization`；需要打补丁、重编译工具链/服务 → `go/go-runtime-patch-build`
- Sonic JSON encode/decode 池与 buffer 复用 → `sonic-go/sonic-go-pool-optimization`
- 数据面转发路径要改代码 → `code-optimizer` 侧 `dpdk-optimization`
- nginx 场景 → 先由本侧 `nginx-baseline-setup` 建立 L0 基线与循环纪律，再进入 `nginx-bottleneck-analysis` 与分级优化 skill
- 转发/Web 服务只调配置与系统参数 → 本侧 nginx 调优链（`nginx-baseline-setup` → `nginx-bottleneck-analysis` → 环境级/配置级/加速级） / `kptools-dpdk-tuning`
- 需要改业务代码/算法 → `code-optimizer` 侧 `microservice/`

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/microservice/**`
