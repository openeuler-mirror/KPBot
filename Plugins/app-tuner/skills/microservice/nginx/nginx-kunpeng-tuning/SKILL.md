---
name: nginx-kunpeng-tuning
description: Nginx 性能优化·加速级（调优链第三环之一，最高收益也是最高风险级别）。当 nginx https 场景在配置级/环境级优化后握手吞吐仍不达标，或纯 HTTP 场景已确认 glibc arena/malloc 分配竞争（tcmalloc 分支），需要换库/硬件加速时使用本 skill：KAL OpenSSL（鲲鹏加速版 OpenSSL，运行期注入）、KAE/UADK 硬件加速引擎（hisi_hpre，需鲲鹏加速器硬件）、tcmalloc（替换 glibc malloc）、xcall 微架构开关。所有步骤执行前置快照、A/B 验证收益后才保留、失败立即回退；共享/多租户节点须先确认占用。命令自包含。
---

# Nginx 加速级优化（鲲鹏硬件/运行时加速）

> 经验数据来源：鲲鹏平台 nginx 压测实测（原始记录见 kpbot-dev-journal `a+k/bale/nginx/`）。

**链路定位**：调优链最高级别，仅在配置级（`../nginx-conf-tuning/`）与环境级（`../nginx-env-tuning/`）优化后仍不达标、且瓶颈分析（`../nginx-bottleneck-analysis/`）确认收益空间在加解密/内存分配时进入。A/B 验证纪律与基线复测见 `../nginx-baseline-setup/`。

**KAE/UADK 提示**：hisi_hpre 密码卸载与 `kpbot-app-tuner` 插件的 `accelerator-optimization` skill 同域（源码树 `skills/kpbot-app-tuner/subskills/accelerator-optimization/`；安装后 `<config>/skills/kpbot-app-tuner/subskills/accelerator-optimization/`）——该 skill 提供 UADK 引擎判定、使能与验证的完整工作流（含采集/使能/验证脚本），涉及 KAE 引擎配置与 uacce 设备判定时优先引用之；若该 skill 尚未合入，playbook 的 KAE/UADK 节提供最小采集与验证命令。本 skill 聚焦 nginx 场景的加速组合与风险纪律。

约定占位符：`$NGINX_DIR`、`$CONF`、`$MASTER_CORE`、`$SNAP`（回退快照目录）。

## 1. 触发条件

- 瓶颈分析确认 TLS 握手/加解密热点（`RSA_*`/`EVP_*` 聚合 ≥ 20%）且配置级（ECDSA/TLS1.3/session 复用）已做仍不达标，现场已具备 KAL OpenSSL 部署或鲲鹏加速器硬件
- 内存分配竞争热点（glibc arena 锁，`malloc`/`free` 聚合 ≥ 10%）需要 tcmalloc
- 独占环境且基线已存档，评估 xcall 微架构开关

## 2. 加速项清单（按风险与可用性排序）

| 加速项 | 收益场景 | 风险级别 | 关键风险 |
|--------|----------|----------|----------|
| KAL OpenSSL | https 握手核心收益 | 高 | LD_LIBRARY_PATH 全局生效、与发行版 OpenSSL 混链可能符号冲突/ABI 不一致；回退靠恢复原环境与二进制 |
| KAE / UADK | 需鲲鹏加速器硬件 | 高（全机） | 驱动卸载影响全机占用进程；uacce_mode 改设备模式 |
| tcmalloc | 分配竞争场景（A/B 为准） | 中 | LD_PRELOAD 影响全部子进程；库版本不匹配可段错误 |
| xcall | 微架构级 | 最高 | 非公开接口、影响同核其他进程、无回执；共享节点禁止 |

KAL 说明：本 skill 只承载**运行期注入**（LD_LIBRARY_PATH/PATH + ldd 生效验证 + 回退）；KAL 源码/制品的获取渠道未明确，不在本 skill 内维护——现场已具备 KAL 部署（源码、路径与环境说明由用户或内部渠道提供）时才进入该项。

xcall 说明：开关序列属实测经验值（原始记录见 kpbot-dev-journal），**未在本 skill 内维护**——在独占环境确需使用时按现场原始记录执行，本 skill 只约束其风险纪律（快照基线、共享节点禁止、重启消除、A/B 验证）。

**Safety Gates（全项共用）**：每步必须——先快照（L3.0 前置快照，见 references）→ 再变更 → 验证生效（ldd/maps/engine 自检）→ A/B 证实收益后才保留；任一环节失败立即按回退步骤恢复，禁止跳步叠加。多租户/共享节点上驱动与环境变量类变更影响全机，执行前必须确认占用情况。

## 3. 各加速项命令

运行期注入/驱动切换/生效验证/回退的完整命令（KAL 运行期注入与 ldd 验证、KAE 驱动切换与引擎自检、tcmalloc 安装注入与 maps 验证）见 `references/kunpeng-accel-playbook.md`。

## 4. 输出契约

- `accel_changes`：已实施加速项 + 快照目录路径 + 生效验证证据（ldd/sha256/maps 输出）
- `gain_vs_baseline`：A/B 对比（L0 → +配置级/环境级 → +加速级）
- 无增益或验证失败项按回退命令恢复，并在结果中注明失败环节

## 参考资料

- `references/kunpeng-accel-playbook.md` — 前置快照与四项加速的完整命令、生效验证与回退
