# TP 拓扑变更原理与风险详解

本文是 kptools-tpsplit-optimization 的领域知识底座。动手改拓扑前理解这些"为什么"，能在现场快速判断异常该往哪查。

## 1. TP 变更的本质：跨机 TP32 → 单机 TP8

- 旧方案：一个 Prefill 引擎跨多台机器，32 卡做张量并行，跨机 NVLink / 高速互联压力大，依赖 Ray 做集群调度
- 新方案：**TP 严格单机闭环**，单引擎最大 8 卡，TP 通信不出本机
- 代价：单 Prefill 引擎的单请求最大算力 / 显存池变小（max batch、单序列长度上限下降）
- 收益：
  - 跨机 TP 通信抖动消失（TP 通信延迟直接决定 Prefill 的 token 吞吐下限）
  - 故障域收缩：一台引擎挂了不牵连其他 TP 组（TP32 跨机时一台机器故障 = 整个引擎全挂）
  - 部署更简单，去掉 Ray 依赖

## 2. 两层 DP 的区别（最容易搞混的概念）

| 概念 | 配置位置 | 含义 | 例子 |
| --- | --- | --- | --- |
| vLLM 引擎组 DP | `--data-parallel-size=4` | **4 个独立 TP8 引擎组成一个 DP 组**，做 Prefill 的请求数据并行分流 | 组内 rank 0/1/2/3，统一主节点 IP，rpc-port=16600 |
| Mooncake 全局 dp_size | KV_TRANSFER_CONFIG 的 `prefill.dp_size` | **全局所有 Prefill DP 副本总数**（跨多台主机、多 DP 组累加），Mooncake 用它做 KV 的目标寻址 | dp_size=20 = 20 个 TP8 实例，Proxy 把请求分发到这 20 个实例 |

写错任意一个的后果：KV 寻址异常、丢 KV、偶发 NPU 报错。

## 3. Mooncake KV 传输对齐（非常容易踩坑）

Mooncake 是 Decode 和 Prefill 之间传输 KV Cache 的层，它的分片规则**必须和 Prefill 侧 TP 拓扑严格匹配**：

- `prefill.tp_size=8`：KV 在 Prefill 侧按 TP8 切分
- `prefill.dp_size=20`：Mooncake 知道一共有 20 个 Prefill worker，用来做 KV 的目标寻址

⚠️ 错配后果：KV 路由错乱、丢 KV、Decode 拿错 KV 分片、偶发 NPU 报错 / hangs。注意这些**不是启动期报错**，而是运行期才暴露的静默错误，排查成本极高——所以配置一致性自检要前置到部署的每个阶段。

同步范围：所有 **P 侧实例** 和所有 **D 侧实例** 的 KV_TRANSFER_CONFIG 都要改（D 侧配置里也有 prefill 段）。新旧拓扑混跑等价于错配。

## 4. 单机双实例与卡隔离

单机内部可以跑 **2 个独立 TP8 vLLM Prefill 服务**（16 卡机），两个实例互不干扰，但必须做好 `ASCEND_RT_VISIBLE_DEVICES` 切分：

- 实例 0 绑定 0-7 卡，实例 1 绑定另一组 8 卡
- ⚠️ 两实例看到同一批卡 = 资源冲突，实例起不来或互相干扰
- 同机实例需要错开的只有 http 端口（9000/9001）和卡绑定；KV 配置里的 kv_port / lookup_rpc_port / engine_id **不涉及变动**，沿用旧值
- Proxy 网关把流量轮询 / 按负载分到 9000、9001，实现单机内 Prefill 的水平分流

## 5. 显式 DP 组网（替代 Ray 托管）

- 旧方案用 Ray 自动发现、托管 DP rank；新方案不用 Ray
- 必须手动分配：每个 DP 组内 4 个实例，分配 rank 0/1/2/3（`--data-parallel-rank`）、统一主节点 IP（`--data-parallel-address`）、固定 rpc-port=16600（`--data-parallel-rpc-port`，组内所有实例必须使用同一端口）
- 启动脚本上，每个 TP8 实例单独指定自己的 rank，**不能再一套脚本拉起整个 TP+DP 集群**（这正是去掉 Ray 的代价：编排从自动变手动，换来的是更少的依赖和更小的故障域）
- 主节点（rank 0）要先起，其余实例向它 rendezvous；主节点没起其余实例会组网失败
- **形态自适应**：本节描述的是"旧方案用 Ray"的典型迁移。旧方案本身没有 Ray 时（mp 后端、无旗标、或已是单机 TP），"去 Ray"相关步骤自然跳过——新方案目标不变，只是迁移差异项更少

## 6. 重点风险 & 上线检查清单

1. ✅ 卡隔离：`ASCEND_RT_VISIBLE_DEVICES` 两台 TP8 实例不能看到同一批卡，会资源冲突
2. ✅ DP 组通信：16600 端口（rpc-port）组内互通，防火墙 / 安全组放行
3. ✅ Mooncake 配置同步：所有 P 侧、D 侧的 KV_TRANSFER_CONFIG tp/dp 必须同步更新，不能新旧拓扑混跑
4. ✅ 去掉 Ray 依赖：启动命令删掉 `--distributed-executor-backend ray`，不要残留 Ray 环境变量（旧方案本身无 Ray 时此项跳过）
5. ✅ 端口规划：9000/9001 是 vLLM http 服务端口，16600 是 DP 内部 rpc 通信端口，避免冲突
6. ✅ 全局 DP 总数：Mooncake dp_size=20 时，要确认所有主机上 TP8 实例加起来正好 20 个，数目对不上 KV 寻址异常
7. ✅ 负载模型变化：单 TP8 的最大 batch、单序列长度上限相比 TP32 下降，需要重新压测 Prefill 吞吐、P-D KV 传输带宽

## 7. 典型症状 → 排查方向速查

| 症状 | 优先排查 |
| --- | --- |
| 实例起不来，日志报卡/设备相关错误 | ASCEND_RT_VISIBLE_DEVICES 是否与其他实例重叠；npu-smi 确认卡健康 |
| 实例起不来，日志报端口/bind 相关错误 | http 端口 / 16600 是否被占 |
| DP 组内实例组网失败 | rank 0（主节点）是否先启动；16600 是否互通；主节点 IP 配置是否一致 |
| 运行期丢 KV / Decode 拿错分片 / 偶发 NPU 报错 / hangs | Mooncake prefill tp/dp 与实际拓扑是否一致；是否有实例还在跑旧拓扑配置 |
| 请求 500 / No instance | Proxy 后端列表是否已更新为新实例地址 |

## 8. 为什么先梳理配置调用链，再动参数

"改 p_dp_size / p_tp_size"说的是**改什么**，不是**改哪**。线上参数的真正定义处可能不在启动脚本里：

- 脚本 `source` 的共享 env 文件（P/D 两侧可能 source 同一份拓扑配置）
- systemd `Environment=` / `EnvironmentFile=`、容器 env、父级编排脚本传参

直接改下游脚本的两种事故：

1. **被覆盖**：上游刷新（重新部署、重启 unit、容器重建）把你的修改冲掉，"改了又没改"
2. **漏改消费方**：D 侧或其他实例仍从原文件读旧值——新旧拓扑混跑，KV 错配当场发生，且表现是运行期静默错误（见 §3），现场极难定位到"改错了地方"

正确做法：追调用链（source 引用 → 参数定义处 → 拉起方式 → 消费方清单），产出《配置修改点清单》，**改源头文件**；只有现场确实是参数写死在脚本内的自包含形态，才直接改脚本。
