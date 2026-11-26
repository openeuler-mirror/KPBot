# BIOS Optimization Playbook

本文件承接 `bios-optimization/SKILL.md` 的细节。进入 BIOS 参数分析、平台识别、Redfish 属性归一化、证据质量分级或 critical 回退规划时读取。

## Scope

- **平台**: ARM aarch64 鲲鹏 916/920/930/950 + 昆仑 BIOS / AMI Aptio / InsydeH2O
- **不纳入**: x86、非鲲鹏 ARM、其他 OS
- **职责**: BIOS/固件层参数调优（只分析不执行）
- **不包含**: OS 内核参数 → `os-optimization`;线程绑核 → `cpu-affinity-optimization`;应用参数 → `application-config-optimization`
- **内核版本不敏感**: BIOS 选项由固件决定,与 openEuler 内核版本（OLK-5.10/OLK-6.6）无关
- **证据时效性**: 不检查。BIOS 设置存储在 NVRAM/CMOS,不受运行时进程影响

## Knowledge Base Anchors

| 技术 | 适用信号 | 案例/收益口径 | 验证指标 |
|---|---|---|---|
| Power Profile → Performance | Power Profile=Balanced/PowerSave | 经验推荐,具体收益取决于实际测试环境 | TPS、P95 |
| SMT → off（920 无 SMT） | 920 平台 + 数据库 | 920 无 SMT,不涉及;930/950 SMT2 经验推荐关闭,尾延迟抖动减少 | TPS、上下文切换、P95 |
| Node Interleaving → off | Node Interleaving=on + NUMA 敏感应用 | 消除 NUMA 模糊,恢复拓扑感知 | numactl -H、跨节点访存 |
| C-State → C1 | C-State=C6 + OLTP 延迟敏感 | 减少唤醒延迟,经验推荐 | cpuidle residency、P95 |
| DDR Speed → 标称最高 | DDR 降频 | 恢复内存带宽,经验推荐 | 内存带宽、TPS |
| PCIe ASPM → off | ASPM=on + NVMe 延迟敏感 | 减少 PCIe 链路状态切换延迟 | NVMe 延迟、IOPS |
| Hardware Prefetcher → on | 默认（一般已 on） | 顺序扫描受益;随机访问影响小 | cache miss rate |

---

## A+K 场景 BIOS 经验库

> **⚠️ 本节经验值用于对照判断,不是可直接输出给用户的答案。**
> 必须先通过 Redfish 采集或 OS 侧命令获取当前 BIOS 实际配置后,将当前值与本节推荐值对照,才能生成 candidate_actions。
> 例如: 采集到当前 Power Profile=Balanced → 对照推荐值 Performance → 输出"当前 Balanced,推荐改为 Performance"。
> 禁止在未采集到当前配置数据前,直接将本节推荐值作为分析结果输出给用户。

> A+K = Ascend + Kunpeng（昇腾 NPU + 鲲鹏 CPU 组合场景）。基于鲲鹏 920/930/950 实际调优经验沉淀,适用于 AI 训练/推理等计算密集型工作负载。

### 核心经验（6 项）

| # | 参数 | 推荐设置 | 理由 | 适用型号 | 生效方式 | 风险 |
|---|------|---------|------|---------|---------|------|
| 1 | **Power Profile** | **Performance** | 最大化 CPU 性能,禁用节能特性,避免频率降档。鲲鹏 950 动态频率（1.2-2.3GHz）下影响最大 | 全型号 | 冷重启（部分昆仑 BIOS 支持在线切换） | low |
| 2 | **SMT** | **Off** | 关闭超线程。数据库/中间件场景下 SMT 线程争抢导致尾延迟抖动;关闭后逻辑核数减半但单核性能提升,TPS 更稳定 | 930/950（920 无 SMT） | 冷重启 | medium |
| 3 | **Turbo (Core)** | **On** | 开启 Core Turbo Boost。鲲鹏 950 动态频率下 Turbo 提升峰值性能;930 受限 Turbo 也有正向收益。Power Profile=Performance 时 Turbo 默认 on | 930/950（916/920 无 Turbo） | 冷重启 | low |
| 4 | **Turbo (Uncore)** | **On** | 开启 Uncore Turbo。Uncore 频率影响内存控制器和 L3 缓存性能,关闭后 Uncore 频率降档导致内存带宽和缓存吞吐下降。BIOS 支持 Uncore Turbo 时应与 Core Turbo 同时开启 | 930/950（需 BIOS 支持 Uncore Turbo 配置项） | 冷重启 | low |
| 5 | **UFS** | **Off** | 关闭 UFS（Unified Fabric Switch）。UFS 在某些鲲鹏平台上用于互联调度,开启后会引入额外的拓扑调度开销,对数据库/中间件等 NUMA 敏感工作负载产生负向影响 | 需 BIOS 支持 UFS 配置项 | 冷重启 | medium |
| 6 | **LPI** | **Off** | 关闭 LPI（Low Power Idle）。LPI 是 ARM 架构的深度低功耗状态,开启后 CPU 进入/退出低功耗状态产生唤醒延迟,对延迟敏感型工作负载（数据库 OLTP、RPC）造成尾延迟抖动。关闭后消除唤醒延迟开销 | 需 BIOS 支持 LPI 配置项 | 冷重启 | low |

> 以上 6 项为 A+K 场景经验推荐,具体收益取决于实际测试环境,建议变更后通过压测验证。

### 经验决策规则

```
A+K 场景 BIOS 核心配置检查（优先级从高到低）:

1. Power Profile:
   IF Power Profile != Performance:
     → 推荐改为 Performance
     → 联动: Turbo 默认 on, C-State 自动限制
     → 收益: 有正向收益,具体取决于实际测试环境
   IF Power Profile == Performance:
     → 跳过（已最优）

2. SMT:
   IF 鲲鹏型号 == 916 或 920:
     → 跳过（平台无 SMT）
   IF 鲲鹏型号 == 930 或 950:
     IF SMT == on:
       → 推荐改为 off
       → 联动: 逻辑核数减半,需重跑 cpu-affinity-optimization
       → 收益: 数据库场景尾延迟抖动减少,TPS 更稳定,具体取决于实际测试环境
     IF SMT == off:
       → 跳过（已最优）

3. Turbo (Core):
   IF 鲲鹏型号 == 916 或 920:
     → 跳过（平台无 Turbo）
   IF 鲲鹏型号 == 930 或 950:
     IF Turbo (Core) == off:
       → 推荐改为 on
       → 若 Power Profile 已改为 Performance: Turbo 可能已被联动开启,标注 triggered_by
     IF Turbo (Core) == on:
       → 跳过（已最优）

4. Turbo (Uncore):
   IF BIOS 不支持 Uncore Turbo 配置项:
     → 跳过（平台不支持）
   IF BIOS 支持 Uncore Turbo 配置项:
     IF Turbo (Uncore) == off:
       → 推荐改为 on
       → 联动: 应与 Core Turbo 同时开启
     IF Turbo (Uncore) == on:
       → 跳过（已最优）

5. UFS:
   IF BIOS 不支持 UFS 配置项:
     → 跳过（平台不支持）
   IF BIOS 支持 UFS 配置项:
     IF UFS == on:
       → 推荐改为 off
       → 收益: 减少 NUMA 敏感工作负载的拓扑调度开销
     IF UFS == off:
       → 跳过（已最优）

6. LPI:
   IF BIOS 不支持 LPI 配置项:
     → 跳过（平台不支持）
   IF BIOS 支持 LPI 配置项:
     IF LPI == on:
       → 推荐改为 off
       → 收益: 消除深度低功耗状态唤醒延迟,减少尾延迟抖动
     IF LPI == off:
       → 跳过（已最优）
```

### 参数联动关系

```
Power Profile=Performance
  ├── Turbo (Core) → 通常默认 on（取决于 BIOS 厂商实现,非必然联动）
  ├── Turbo (Uncore) → 应与 Core Turbo 同时开启（BIOS 支持时）
  └── C-State → 通常自动限制为浅 C-State（取决于 BIOS 厂商实现）

SMT=off
  └── 逻辑核数减半 → NUMA 拓扑变化 → cpu-affinity-optimization 需重跑
      （findings 标注 requires_cpu_affinity_rerun=true）

UFS=off
  └── 恢复原生 NUMA 拓扑 → cpu-affinity-optimization 需重跑
      （findings 标注 requires_cpu_affinity_rerun=true）
      （UFS 是独立变更项,与 Power Profile 无联动）

LPI=off
  └── CPU 不再进入深度低功耗状态 → 与 C-State 限制协同生效
      （LPI 是独立变更项,但与 C-State 策略方向一致: 都是为了减少唤醒延迟）
```

### 预期收益

> 以下为经验定性判断,具体收益取决于实际测试环境（应用类型、并发度、NUMA 拓扑、存储介质等）。变更后应通过压测验证实际收益。

| 工作负载 | Power Profile→Performance | SMT→Off | Turbo (Core)→On | Turbo (Uncore)→On | UFS→Off | LPI→Off |
|---------|--------------------------|---------|-----------------|-------------------|---------|---------|
| 数据库 OLTP | 有正向收益 | 有正向收益,尾延迟抖动减少 | 有正向收益（950 动态频率下更明显） | 有正向收益,内存带宽和缓存吞吐提升 | 有正向收益,减少拓扑调度开销 | 有正向收益,消除唤醒延迟抖动 |
| 中间件/RPC | 有正向收益 | 有正向收益,延迟抖动减少 | 有正向收益 | 有正向收益 | 有正向收益 | 有正向收益,减少尾延迟抖动 |
| 通用服务器 | 有正向收益 | 按实测 | 有正向收益 | 有正向收益 | 按实测 | 按实测 |

> Power Profile 与 Core Turbo 存在联动关系: Power Profile=Performance 时 Turbo 通常默认为 on（取决于 BIOS 厂商实现,非必然联动）。Uncore Turbo 应与 Core Turbo 同时开启。UFS 是独立变更项,与 Power Profile 无联动。建议逐项变更并压测,以确定每项的实际贡献。

### 验证方法

| 参数 | 变更前记录 | 重启后验证 |
|------|-----------|-----------|
| Power Profile | Redfish BIOS Attributes 或 BIOS Setup 截图 | 重新读取 Redfish BIOS Attributes |
| SMT | `cat /sys/devices/system/cpu/smt/active` | 同（应从 1 变为 0） |
| Turbo | `cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq` | 同（确认 max freq 未变或更高） |
| UFS | Redfish BIOS Attributes | 重新读取 Redfish BIOS Attributes |
| LPI | Redfish BIOS Attributes 或 `cat /sys/devices/system/cpu/cpuidle/state*/name` | 重新读取 Redfish 或确认深度 idle 状态已移除 |

### 回退方案

| 参数 | 回退方式 | 风险 |
|------|---------|------|
| Power Profile | 恢复原 profile（如 Balanced） | low,冷重启恢复 |
| SMT | 恢复 SMT=on | medium,冷重启恢复 + 需重跑 cpu-affinity |
| Turbo (Core) | 恢复 Turbo=off | low,冷重启恢复 |
| Turbo (Uncore) | 恢复 Uncore Turbo=off | low,冷重启恢复 |
| UFS | 恢复 UFS=on | medium,冷重启恢复 + 需重跑 cpu-affinity |
| LPI | 恢复 LPI=on | low,冷重启恢复 |

### 与其他参数的关系

A+K 核心经验（Power Profile / SMT / Turbo / UFS / LPI）之外的 BIOS 参数按现有 Decision Matrix 处理,不强制变更:

| 参数 | A+K 核心经验是否覆盖 | 默认推荐 |
|------|-------------------|---------|
| Node Interleaving | ❌ 不在核心 3 项,但推荐 off | off |
| C-State | ❌ Power Profile=Performance 会联动限制 | C1（OLTP）/ OS controlled（Batch） |
| DDR Speed | ❌ 不在核心 3 项,但推荐标称最高 | 标称最高 |
| Hardware Prefetcher | ❌ 不在核心 3 项 | on |
| PCIe ASPM | ❌ 不在核心 3 项 | off（数据库场景） |

---

## P1-A: 平台感知

### Item 1: BIOS 厂商识别

| 厂商 | 识别方式 | 常见服务器 | Redfish 支持度 |
|------|---------|-----------|---------------|
| 昆仑 BIOS | `dmidecode -t bios` → "Kunpeng" 或 "Huawei" | TaiShan 200/2280 | ✅ 完整 |
| AMI Aptio | `dmidecode -t bios` → "American Megatrends" | 部分 TaiShan V5、第三方主板 | ✅ 完整（属性名不同） |
| InsydeH2O | `dmidecode -t bios` → "Insyde" | 少数定制服务器 | ✅ 完整（属性名不同） |

```
IF dmidecode 无法获取厂商:
  → 尝试 /sys/class/dmi/id/bios_vendor
  → 仍无法获取: platform_unknown=true,降级 analysis_only
```

### Item 2: 鲲鹏 4 代处理器 BIOS 选项差异矩阵

| BIOS 选项 | 916 (0xd01) | 920 (0xd01) | 930 (0xd03) | 950 (0xd06) |
|---------|-------------|-------------|-------------|-------------|
| Power Profile | 可配 | 可配 | 可配 | 可配（动态频率,影响更大） |
| SMT | ❌ 无 SMT | ❌ 无 SMT | ✅ SMT2 | ✅ SMT2 |
| Node Interleaving | 可配 | 可配 | 可配 | 可配 |
| C-State | 基础 | C0/C1/C6 | C0/C1/C6（早期 BIOS 不可配） | C0/C1/C6 |
| Turbo | ❌ 无 | ❌ 无 | 受限 | ✅ 动态频率 |
| DDR Speed | 固定 | 固定 2666/2933/3200 | 可配 | 可配 |
| Hardware Prefetcher | 可配 | 可配 | 可配 | 可配 |
| PCIe ASPM | 可配 | 可配 | 可配 | 可配 |
| Energy Performance Bias | ❌ ARM 无 | ❌ ARM 无 | ❌ ARM 无 | ❌ ARM 无 |

```
IF 鲲鹏型号 == 916 或 920:
  → SMT 参数: 跳过（平台不支持）
  → Turbo 参数: 跳过（平台无 Turbo）
  → EPB 参数: 跳过（ARM 无此功能）
IF 鲲鹏型号 == 930:
  → 检查 BIOS 版本（早期版本 C-State 不可配,见 Item 18）
IF 鲲鹏型号 == 950:
  → Turbo 有实际意义（动态频率）
  → Power Profile 影响最大（1.2-2.3GHz 动态范围）
```

### Item 3: Redfish 属性名映射表

各厂商 BIOS 对同一功能用不同的 Redfish 属性名。

| 功能 | 昆仑 BIOS | AMI Aptio | InsydeH2O | 通用关键词 |
|------|----------|-----------|-----------|-----------|
| Power Profile | `WorkloadProfile` | `SystemProfile` | `PowerProfile` | `workload`/`profile`/`power_profile` |
| SMT | `ProcHyperthreading` | `LogicalProc` | `HyperThreading` | `hyperthread`/`smt`/`logical_proc` |
| C-State | `ProcessorCstate` | `CStateCtl` | `CstateEnable` | `cstate`/`c_state` |
| Turbo | `ProcTurbo` | `TurboMode` | `TurboBoost` | `turbo` |
| NUMA/Interleaving | `NumaGroupSizeOpt` | `NodeInterleave` | `NodeInterleaving` | `numa`/`interleav` |
| DDR Speed | `DDRSpeed` | `MemFreq` | `MemorySpeed` | `ddr`/`memory_speed`/`mem_freq` |
| Hardware Prefetcher | `HWPrefetcher` | `Prefetcher` | `HwPrefetch` | `prefetch` |
| PCIe ASPM | `PcieAspmSupport` | `AspmControl` | `PCIeASPM` | `aspm`/`pcie_power` |

### Item 3b: Redfish 属性名归一化策略

**匹配策略（三级降级）**:

```
1. 精确匹配: 在 Redfish BIOS Attributes JSON 中查找映射表中的已知属性名
   → 命中: source=redfish, confidence=high
   → 未命中: 进入第 2 级

2. 关键词模糊匹配: 用通用关键词在所有属性名中做大小写不敏感的子串匹配
   → 命中唯一属性: source=redfish, confidence=high, 标注 matched_by=keyword
   → 命中多个属性: 取第一个 + 标注 ambiguous_match=true, confidence=medium
   → 未命中: 进入第 3 级

3. 降级到 OS 侧推断:
   → source=os_inferred, confidence=medium
   → 若 OS 侧也无法推断: source=unavailable, 跳过该参数
```

**约束**:
- 不得伪造 BIOS 状态。无法匹配时必须标注 `unable_to_determine`
- 模糊匹配结果在 findings 中记录 `matched_property_name` 和 `match_method`
- 同一参数在 Redfish JSON 中找到多个候选属性时,不猜测,标注 `ambiguous_match`

### Item 4: OS 侧 BIOS 推断规则

无 Redfish 时,从 OS 侧命令反推 BIOS 当前设置:

| BIOS 参数 | OS 侧推断方式 | 能否推断 | 置信度 |
|---------|-------------|---------|--------|
| Power Profile | 无法从 OS 侧推断 | ❌ | — |
| SMT | `cat /sys/devices/system/cpu/smt/active` | ✅ | medium |
| NUMA/Node Interleaving | `numactl -H`（节点数推断: 1 节点=interleaving on） | ✅ | medium |
| C-State | `cpupower idle-info`（可用 C-State 列表） | ✅ | medium |
| Turbo | `lscpu`（频率范围推断: max > base 则有 Turbo） | ✅ | low |
| DDR Speed | `dmidecode -t memory` → "Configured Memory Speed" | ✅ | high |
| Hardware Prefetcher | 无法从 OS 侧推断 | ❌ | — |
| PCIe ASPM | `lspci -vvv`（LnkCtl: ASPM Disabled/L0s/L1/All） | ✅ | medium |
| BIOS 厂商/版本 | `dmidecode -t bios` / `/sys/class/dmi/id/` | ✅ | high |
| 服务器型号 | `dmidecode -t system` / `/sys/class/dmi/id/` | ✅ | high |

### Item 5: 云环境/虚拟化 BIOS 不可达降级策略

```
IF 云环境（AWS/阿里云/华为云 ECS）或虚拟机（KVM/VMware）:
  → 无 BMC/Redfish 访问
  → OS 侧推断: 可获取 SMT/NUMA/C-State/DDR Speed/ASPM
  → 无法获取: Power Profile/Hardware Prefetcher
  → status=degraded
  → confidence=medium（OS 侧推断）
  → 可分析的参数: 正常分析但标注 inferred_from_os
  → 不可获取的参数: 标注 unable_to_determine,跳过
  → candidate_actions 默认 analysis_only（虚拟化环境下 BIOS 变更不可执行）
```

---

## P1-B: BIOS 参数详细知识

### Power Profile

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | Performance | 最大化 CPU 性能,禁用节能特性 |
| Compute | Performance | 同上 |
| RPC/Latency-sensitive | Performance | 减少频率切换延迟 |
| Batch/Throughput | Performance 或 Custom | Performance 最省心;Custom 可精细调优 |

#### 平台差异

| 鲲鹏型号 | 可配 | 默认值 | 说明 |
|---------|------|--------|------|
| 916 | ✅ | Balanced | 固定 2.4GHz,Power Profile 主要影响 C-State |
| 920 | ✅ | Balanced | 固定 2.6GHz,Power Profile 主要影响 C-State |
| 930 | ✅ | Balanced | 2.2GHz,Power Profile 影响 C-State/Turbo |
| 950 | ✅ | Balanced | 1.2-2.3GHz 动态,Power Profile 影响最大 |

#### 在线生效条件

| 厂商 | 在线切换 | 说明 |
|------|---------|------|
| 昆仑 BIOS（部分版本） | ✅ | Power Profile 可不重启即时切换 |
| AMI Aptio | ❌ | 需冷重启 |
| InsydeH2O | ❌ | 需冷重启 |

> 无法确定是否支持在线切换时,默认 `change_mode=system_reboot`（保守）。

#### 决策规则

```
IF Power Profile == Performance:
  → 跳过（已最优）
  → 其他参数（C-State/Turbo/Prefetcher）从 Performance profile 的默认值派生

IF Power Profile == Custom:
  → Power Profile 本身不输出变更建议
  → 其他参数不假设从 profile 派生,需逐参数独立判断
  → findings 标注 "Power Profile=Custom,各参数独立配置,逐项检查"

IF Power Profile == Balanced/PowerSave:
  → 推荐改为 Performance（或 Custom + 手动设各参数）
  → 若改为 Custom: 输出各参数的推荐值作为单独的 candidate_actions
  → 若改为 Performance: 其他参数跟随 profile 默认值,不输出单独 candidate_actions

IF Power Profile == HPC:
  → 通常已接近最优,逐参数检查是否有偏差
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `WorkloadProfile`/`SystemProfile`/`PowerProfile`（按映射表匹配） | high |
| OS 侧推断 | 无法推断 | — |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- 920 固定频率,Power Profile 对性能影响主要通过 C-State/Turbo 间接体现
- 950 动态频率,Power Profile 直接影响频率范围,收益最大

---

### SMT / Hyper-Threading

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | Off 或按实测 | 数据库线程切换开销;920 无 SMT 不涉及 |
| Compute | On | 计算密集型受益于 SMT |
| RPC/Latency-sensitive | Off 或按实测 | 减少 SMT 线程争抢 |
| Batch/Throughput | On | 吞吐量受益 |

#### 平台差异

| 鲲鹏型号 | 可配 | SMT 类型 | 说明 |
|---------|------|---------|------|
| 916 | ❌ | 无 SMT | 跳过此参数 |
| 920 | ❌ | 无 SMT | 跳过此参数 |
| 930 | ✅ | SMT2 | 2 线程/核 |
| 950 | ✅ | SMT2 | 2 线程/核 |

#### 决策规则

```
IF 鲲鹏型号 == 916 或 920:
  → 跳过（平台无 SMT）

IF 鲲鹏型号 == 930 或 950:
  IF SMT == 推荐值 → 跳过
  IF SMT != 推荐值 → 推荐改为推荐值
    风险: medium
    生效方式: 冷重启
    依赖: SMT off → 逻辑核数减半 → NUMA 拓扑变化 → 影响 cpu-affinity-optimization
    跨 skill 联动: requires_cpu_affinity_rerun=true
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `ProcHyperthreading`/`LogicalProc`/`HyperThreading` | high |
| OS 侧推断 | `cat /sys/devices/system/cpu/smt/active` | medium |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- 920 无 SMT,不涉及此参数
- SMT 变更影响逻辑核数,必须标注 `requires_cpu_affinity_rerun=true`

---

### NUMA / Node Interleaving

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | Off（暴露 NUMA） | NUMA 感知绑核/绑内存的前提 |
| Compute | Off | 同上 |
| RPC/Latency-sensitive | Off | 同上 |
| Batch/Throughput | Off | 同上 |

> 所有工作负载均推荐 Off。Node Interleaving=on 时 OS 只看到单个 NUMA 节点,NUMA 感知优化全部失效。

#### 平台差异

| 鲲鹏型号 | 可配 | 默认值 | 说明 |
|---------|------|--------|------|
| 916 | ✅ | Off | 简单拓扑 |
| 920 | ✅ | Off | 4 NUMA 节点（每 SCCL=1 node） |
| 930 | ✅ | Off | SNC 模式可配 |
| 950 | ✅ | Off | 192 核,NUMA 拓扑影响大 |

#### 决策规则

```
IF Node Interleaving == off → 跳过（已最优）
IF Node Interleaving == on:
  → 推荐改为 off
  风险: medium
  生效方式: 冷重启
  依赖: 关闭后 OS 感知到真实 NUMA 拓扑（numactl -H 节点数增加）
  跨 skill 联动: requires_cpu_affinity_rerun=true, requires_os_optimization_rerun=true
  验证: 重启后 numactl -H 确认节点数
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `NumaGroupSizeOpt`/`NodeInterleave`/`NodeInterleaving` | high |
| OS 侧推断 | `numactl -H`（1 节点=interleaving on;多节点=off） | medium |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- 920 某版本不支持 SNC（Sub-NUMA Clustering）,见 Item 18

---

### C-State 限制

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | C0 或 C1 | 避免 C6 唤醒延迟（微秒级 → 影响尾延迟） |
| Compute | C6 可接受 | 计算密集,C-State 影响小 |
| RPC/Latency-sensitive | C0 或 C1 | 同 OLTP |
| Batch/Throughput | OS controlled | 允许内核自行管理 |

#### 平台差异

| 鲲鹏型号 | 可配 | 可用状态 | 说明 |
|---------|------|---------|------|
| 916 | ✅ | C0/C1/C6 | 基础 C-State |
| 920 | ✅ | C0/C1/C6 | C6 唤醒延迟较高 |
| 930 | ✅（早期 BIOS ❌） | C0/C1/C6 | 早期 BIOS 版本 C-State 不可配,见 Item 18 |
| 950 | ✅ | C0/C1/C6 | 动态频率下 C-State 影响更大 |

#### 决策规则

```
IF C-State == 推荐值 → 跳过
IF C-State != 推荐值:
  → 推荐改为推荐值
  风险: low（可恢复）
  生效方式: 可能即时或下次重启（因厂商而异）
  依赖: Power Profile=Performance → 可能自动限制 C-State（联动关系）
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `ProcessorCstate`/`CStateCtl`/`CstateEnable` | high |
| OS 侧推断 | `cpupower idle-info`（可用 C-State 列表） | medium |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- 930 早期 BIOS 版本 C-State 不可配,需升级固件
- Power Profile=Performance 可能自动限制 C-State,无需单独操作

---

### Hardware Prefetcher

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | On | 顺序扫描受益（全表扫描、索引扫描） |
| Compute | On | 计算密集,预取减少 stall |
| RPC/Latency-sensitive | On | 一般受益 |
| Batch/Throughput | On | 顺序处理受益 |

> 一般默认为 On,通常不需要变更。随机访问密集场景可考虑 Off,但需实测验证。

#### 平台差异

| 鲲鹏型号 | 可配 | 包含 | 说明 |
|---------|------|------|------|
| 916 | ✅ | L1/L2 Prefetcher | 基础预取 |
| 920 | ✅ | L1/L2 DCU/MLC Spatial Prefetcher | 鲲鹏特色预取器 |
| 930 | ✅ | 同 920 + 增强 | — |
| 950 | ✅ | 同 930 | — |

#### 决策规则

```
IF Hardware Prefetcher == on → 跳过（一般已最优）
IF Hardware Prefetcher == off:
  → 推荐改为 on
  风险: low
  生效方式: 冷重启
  依赖: 无
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `HWPrefetcher`/`Prefetcher`/`HwPrefetch` | high |
| OS 侧推断 | 无法推断 | — |
| 用户手动 | BIOS Setup 截图 | low |

> Hardware Prefetcher 无法从 OS 侧获取,无 Redfish 时需用户手动提供。

#### 已知限制

- 无法从 OS 侧推断,Redfish 不可用时必须用户手动提供

---

### Turbo Boost

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| 所有 | On（如平台支持） | 最大化峰值性能 |

#### 平台差异

| 鲲鹏型号 | 可配 | Turbo 支持 | 说明 |
|---------|------|-----------|------|
| 916 | ❌ | 无 Turbo | 跳过此参数 |
| 920 | ❌ | 无 Turbo | 跳过此参数 |
| 930 | ✅ | 受限 Turbo | Turbo 范围有限 |
| 950 | ✅ | ✅ 动态频率 | 1.2-2.3GHz,Turbo 有实际意义 |

#### 决策规则

```
IF 鲲鹏型号 == 916 或 920:
  → 跳过（平台无 Turbo）
IF 鲲鹏型号 == 930 或 950:
  IF Turbo == on → 跳过
  IF Turbo == off → 推荐改为 on
    风险: low
    生效方式: 冷重启
    依赖: Power Profile=Performance → Turbo 默认 on（联动关系）
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `ProcTurbo`/`TurboMode`/`TurboBoost` | high |
| OS 侧推断 | `lscpu`（max MHz > base MHz 则有 Turbo） | low |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- 920 无 Turbo,不涉及此参数
- 930 Turbo 范围有限,收益较小

---

### DDR Speed / Memory Refresh

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| 所有 | 标称最高频率 | 最大化内存带宽 |

#### 平台差异

| 鲲鹏型号 | 支持最高 | 说明 |
|---------|---------|------|
| 916 | DDR4 2400 | 固定 |
| 920 | DDR4 2666/2933/3200 | 按型号子型号不同 |
| 930 | DDR4 2933/3200 | 可配 |
| 950 | DDR5 4800/5600 | 可配 |

#### 决策规则

```
IF DDR Speed == 标称最高 → 跳过
IF DDR Speed < 标称最高（降频运行）:
  → 推荐恢复标称最高
  风险: high（降频恢复可能影响内存稳定性）
  生效方式: 冷重启
  依赖: 无
  critical 标注: IF DDR Speed 超标称 overclock → risk=critical,change_mode=analysis_only
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `DDRSpeed`/`MemFreq`/`MemorySpeed` | high |
| OS 侧推断 | `dmidecode -t memory` → "Configured Memory Speed" | high |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- DDR Speed 超标称 overclock 属于 critical 风险,可能导致无法启动
- 降频运行可能是内存兼容性问题导致 BIOS 自动降频,恢复标称前需确认内存条规格

---

### Energy Performance Bias

#### 推荐值

不适用。ARM 平台无 Energy Performance Bias（EPB）,这是 Intel x86 专有功能。

```
IF 平台 == ARM（鲲鹏）:
  → 跳过此参数
  → findings 标注 "Energy Performance Bias 不适用于 ARM 平台"
```

---

### PCIe ASPM

#### 推荐值

| 工作负载 | 推荐值 | 理由 |
|---------|--------|------|
| Database OLTP | Off | 减少 NVMe 链路状态切换延迟 |
| Compute | Off 或按实测 | 一般 Off |
| RPC/Latency-sensitive | Off | 减少 PCIe 延迟 |
| Batch/Throughput | Off 或 On | 吞吐量场景可接受 ASPM |

#### 平台差异

| 鲲鹏型号 | 可配 | 说明 |
|---------|------|------|
| 916 | ✅ | 基础 ASPM |
| 920 | ✅ | ASPM L0s/L1 |
| 930 | ✅ | 同 920 |
| 950 | ✅ | 同 930 |

#### 决策规则

```
IF PCIe ASPM == off → 跳过
IF PCIe ASPM == on（数据库/延迟敏感场景）:
  → 推荐改为 off
  风险: low
  生效方式: 冷重启
  依赖: 无
```

#### 证据来源

| 来源 | 获取方式 | 置信度 |
|------|---------|--------|
| Redfish | `PcieAspmSupport`/`AspmControl`/`PCIeASPM` | high |
| OS 侧推断 | `lspci -vvv`（LnkCtl: ASPM Disabled/L0s/L1/All） | medium |
| 用户手动 | BIOS Setup 截图 | low |

#### 已知限制

- ASPM 对 NVMe 延迟有直接影响,数据库场景建议关闭

---

## P1-C: 鲲鹏官方 + 优化参考

### Item 15: 鲲鹏 BoostKit BIOS 调优建议

| 参数 | BoostKit 推荐 | 说明 |
|------|-------------|------|
| Power Profile | Performance | 官方推荐 |
| SMT（930/950） | 按应用实测 | 数据库建议 Off,计算建议 On |
| Node Interleaving | Off | 官方推荐暴露 NUMA |
| C-State | C1（OLTP） | 官方推荐避免 C6 |
| DDR Speed | 标称最高 | 官方推荐 |
| PCIe ASPM | Off | NVMe 场景官方推荐 |

### Item 16: Intel optimization-zone BIOS 经验对照

| BIOS 参数 | Intel 经验（MySQL/PG） | 鲲鹏差异 | 采纳建议 |
|---------|----------------------|---------|---------|
| Power Profile | Performance | 一致 | ✅ 采纳 |
| SMT/Hyper-Threading | MySQL: Off | 920 无 SMT;930/950 按实测 | ⚠️ 平台差异 |
| C-State | C1/C0 | 一致 | ✅ 采纳 |
| Turbo | On | 920 无 Turbo;950 有 | ⚠️ 平台差异 |
| DDR Speed | Max | 一致 | ✅ 采纳 |
| EPB | Max Performance | ARM 无 EPB | ❌ 不适用 |
| PCIe ASPM | Off | 一致 | ✅ 采纳 |

### Item 17: BIOS 参数间依赖关系

| 依赖关系 | 说明 | 处理 |
|---------|------|------|
| Power Profile=Performance → 自动限制 C-State | Performance profile 通常禁用深 C-State | C-State candidate_action 标注 triggered_by Power Profile |
| Power Profile=Performance → Turbo 默认 on | Performance profile 启用 Turbo | Turbo candidate_action 标注 triggered_by Power Profile |
| SMT off → 逻辑核数减半 → NUMA 拓扑变化 | SMT 变更影响 CPU 拓扑 | SMT candidate_action 标注 dependent_action_ids=[NUMA action] |
| 关闭 C-State → Power Profile 可能自动切换 | 部分厂商 BIOS 反向联动 | 检查 Power Profile 是否被联动变更 |
| DDR Speed 降频 → 内存带宽下降 → 影响 HugePages 收益 | 间接影响 os-optimization | DDR Speed 变更标注 requires_os_optimization_rerun=true |

### Item 18: 各型号 BIOS 已知限制

| 型号 | BIOS 版本 | 限制 | 处理 |
|------|---------|------|------|
| 920 | 某版本 | 不支持 SNC（Sub-NUMA Clustering） | NUMA/SNC 参数标注"该 BIOS 版本不支持 SNC",不输出 candidate_action |
| 930 | 早期版本 | C-State 不可配 | C-State candidate_action 标注 change_mode=analysis_only,建议升级固件 |
| 930 | 早期版本 | Turbo 范围受限 | Turbo 收益预期降低 |
| 950 | — | 动态频率范围大,Power Profile 影响最大 | Power Profile 优先级提升 |

```
读取 BIOS 版本（从 bios-info.txt 或 Redfish）
对照已知限制表:
  IF 命中限制:
    → 对应参数标注 "该 BIOS 版本{限制描述}"
    → candidate_action 的 change_mode=analysis_only 或跳过
```

### Item 19: 生产环境 BIOS 变更风险矩阵

| 风险等级 | 操作类型 | 示例 | 回退方式 |
|---------|---------|------|---------|
| low | 可在线生效 | 部分服务器 Power Profile | 恢复原值 |
| medium | 冷重启,可恢复 | SMT on/off、C-State 限制 | 再次重启恢复原设置 |
| high | 冷重启,恢复需谨慎 | DDR Speed 降频恢复、NUMA 拓扑变更 | 记录原值,重启后验证拓扑 |
| **critical** | **可能无法启动** | DDR Speed 超标称 overclock、关闭关键内存通道、BIOS 版本降级 | **见下方 critical 回退计划** |

#### critical 风险操作的回退计划

当 BIOS 变更导致服务器无法启动时,无法通过"恢复原设置"回退,需要硬件级恢复:

1. **CMOS 清除（物理跳线）**: 主板上有 Clear CMOS 跳线/按钮,清除所有 BIOS 设置恢复出厂默认。操作前需记录出厂默认值是否满足要求。
2. **iBMC/IPMI 远程恢复**: 若 BMC 独立于主 BIOS 供电,可通过 BMC Web 界面或 IPMI 命令恢复 BIOS 设置。前提是 BMC 网络可达且凭据已知。
3. **备用 BIOS 芯片切换**: 部分服务器（如 TaiShan 200）支持双 BIOS 芯片,可通过跳线切换到备用芯片启动后修复主 BIOS。

**约束**:
- critical 级别操作的 candidate_actions 必须标注 `risk=critical` 和 `boot_failure_recovery=<cmos_clear|ibmc_remote|backup_chip>`
- critical 操作默认 `change_mode=analysis_only`,即使用户批准执行也不自动生成 Redfish PATCH
- bios_change_plan.md 中对 critical 操作必须包含"无法启动时的恢复步骤"
- standalone 模式下 critical 操作不提供 Redfish PATCH 自动执行选项

---

## P1-D: 证据采集知识

### Item 20: BIOS 必需证据清单 + 来源映射

#### 必需证据清单

| 证据项 | Redfish 属性关键词 | OS 侧推断方式 | 主SKLL已有 | 需补采 |
|--------|------------------|-------------|-----------|--------|
| Power Profile | WorkloadProfile/SystemProfile | 无法从 OS 侧推断 | ✅ bios-redfish-bios.json | — |
| SMT 状态 | ProcHyperthreading/SMT | `/sys/devices/system/cpu/smt/active` | ✅ backup_environment.sh | — |
| NUMA/Node Interleaving | NumaGroupSizeOpt/NodeInterleaving | `numactl -H`（节点数推断） | ✅ backup_environment.sh | — |
| C-State | CStateCtrl/ProcessorCstate | `cpupower idle-info` | ❌ | ✅ 需补采 |
| Turbo Boost | ProcTurbo/TurboBoost | `lscpu`（频率推断） | ✅ lscpu.txt | — |
| DDR Speed | DDRSpeed/MemorySpeed | `dmidecode -t memory` | ✅ hardware-memory.txt | — |
| Hardware Prefetcher | HWPrefetcher | 无法从 OS 侧推断 | ❌ | 需用户手动 |
| PCIe ASPM | PcieAspmSupport | `lspci -vvv` | ❌ | ✅ 需补采 |
| BIOS 厂商/版本 | — | `dmidecode -t bios` / `/sys/class/dmi/id/` | ✅ bios-info.txt | — |
| 服务器型号 | — | `dmidecode -t system` / `/sys/class/dmi/id/` | ✅ bios-info.txt | — |

> "主SKLL已有"指 backup_environment.sh 是否已采集。"需补采"指 collect_bios_evidence.sh 是否需要补采。两个概念不同。

> 主SKLL 的 backup_environment.sh 已采集大部分 BIOS 证据。真正的缺口仅 2 项: C-State（cpupower idle-info）和 PCIe ASPM（lspci -vvv）。Hardware Prefetcher 无法从 OS 侧获取,需用户手动提供。

#### 证据质量分级

| 来源 | 置信度 | 标注 | 说明 |
|------|--------|------|------|
| Redfish BIOS Attributes | high | `source=redfish` | 直接读取 BIOS 当前配置,最准确 |
| OS 侧推断 | medium | `source=os_inferred` | 从 lscpu/numactl/cpupower 反推,部分参数无法推断 |
| 用户手动输入 | low | `source=user_manual` | 用户提供 BIOS 截图或摘录 |
| 无法获取 | — | `source=unavailable` | 对应参数跳过,标注 `unable_to_determine` |

#### BIOS 密码与厂商锁定

| 状态 | 检测方式 | candidate_action 标注 | 处理 |
|------|---------|----------------------|------|
| 需要 BIOS 密码 | Redfish 返回 `BiosPasswordRequired=true` 或用户告知 | `bios_password_required=true` | bios_change_plan.md 标注"需 BIOS 密码",不生成 Redfish PATCH |
| 厂商锁定 | Redfish 属性 `ReadOnly=true` 或用户告知选项灰色 | `locked_by_vendor=true` | 降级为 `analysis_only`,findings 记录"该参数被厂商锁定,不可修改" |
| 正常可改 | Redfish 属性 `ReadOnly=false` 或未标注 | 无特殊标注 | 正常输出 candidate_action |

**约束**:
- 不得忽略 `ReadOnly=true` 标注强行输出可执行动作
- `locked_by_vendor=true` 的参数仍保留在 findings 中（记录发现）,但不进入 `candidate_actions`

---

## Cross-Skill Coordination

### BIOS 变更对其他 subskill 的影响

| BIOS 变更 | 影响的 subskill | 联动说明 | findings 标注 |
|---------|----------------|---------|--------------|
| SMT on → off | `cpu-affinity-optimization` | 逻辑核数减半,绑核方案需重跑 | `requires_cpu_affinity_rerun=true` |
| SMT off → on | `cpu-affinity-optimization` | 逻辑核数翻倍,绑核方案需重跑 | `requires_cpu_affinity_rerun=true` |
| NUMA / Node Interleaving 变更 | `cpu-affinity-optimization` + `os-optimization` | NUMA 拓扑变化,绑内存方案需重跑;`numa_balancing` 策略需重新评估 | `requires_cpu_affinity_rerun=true`, `requires_os_optimization_rerun=true` |
| C-State 限制变更 | `os-optimization` | governor=performance 的效果与 C-State 策略相关 | findings 记录联动 |
| DDR Speed 变更 | `os-optimization` | 内存带宽变化可能影响 HugePages/THP 的收益评估 | `requires_os_optimization_rerun=true` |
| Power Profile 变更 | `os-optimization` | Performance profile 可能自动设置 governor,避免重复调优 | findings 记录联动 |

### os-optimization → bios-optimization 感知

OS SKILL 在分析时会感知 BIOS 限制:
- Node Interleaving=on → os-optimization 建议关闭（归 bios-optimization）
- SMT 状态 → os-optimization 感知（读 smt/active）,影响 IRQ 策略
- C-State → os-optimization 感知 governor=performance 时的 C-State 行为变化
- Power Profile → os-optimization 记录 BIOS 限制,governor 降级为 analysis_only

## Reject Conditions

候选出现以下任一情况应跳过或降级:

- 参数已为推荐值（跳过）
- 平台不支持该参数（920 无 SMT/Turbo/EPB → 跳过）
- 云环境/虚拟化无 BMC → status=blocked
- 非鲲鹏平台 → status=degraded,analysis_only
- 证据完全缺失 → status=blocked
- 厂商锁定（ReadOnly=true）→ 降级 analysis_only
- critical 风险操作 → 默认 analysis_only
- 生产环境不允许重启 → 仅输出建议
- 整体已最优（见 SKILL.md Step 3 整体跳过规则）→ status=ok
