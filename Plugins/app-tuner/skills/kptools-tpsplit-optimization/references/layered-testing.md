# 拉起验证细则（L0-L3）— kptools-tpsplit-optimization 阶段 5

承接 SKILL.md 阶段 5 的压缩表格，展开拉起期每层的执行内容、通过标准与失败处理。拉起期的故障多数不是"起不来"，而是"起来了但 KV 错配"——拉起验证分层铺开，每层有明确通过标准，不过不进下一层。

**范围说明**：本文件只覆盖 L0-L3（拉起验证）。部署完成后的交付验证（L4 功能 / L5 性能对比 / L6 稳定性）由 `kptools-tpsplit-optimization-verify` 承载（独立 PR 交付），细则在 verify 侧维护，本仓不再包含；停服前旧基线采样的时机与口径要求见 SKILL.md 阶段 3 红线。

## L0 静态检查（拉起前）——标准验证产物，固化执行

- `bash -n` 过全部脚本；KV_TRANSFER_CONFIG（嵌在脚本里的字符串）用一段 python 抽出该变量并 `json.loads` 校验合法性
- 实例间 diff：实例级差异只允许 rank / 卡 / http 端口三处（组级差异：DP 主节点 IP / 组大小），其余不一致都是笔误
- 共享 env 形态下：env 文件改动后逐消费方 grep 确认读到的是新值
- 守恒自检（**两种执行口径，先分清再执行**）：
  - **全量目录执行**（扫描目录含全部 P 实例脚本，或各主机目录汇总到一处）：`--expect-dp <全局实例数>`，核对所有实例 prefill.dp_size 一致且等于期望全局值，且**实例清单数必须等于布局表全局实例数**（漏分发/重复分发都 FAIL）；同时核对 DP rank 组级正确性（同组范围 [0,组大小)、组内唯一、组内齐全——rank 在实例 diff 中被遮蔽，此项是重复/越界 rank 的唯一拦截）。**实例总数按 2.1 布局表的全局实例数定义**（Σ 所有主机实例数），不是本机/本目录内的脚本个数
  - **按主机分目录/按主机执行**（每台只放自己的实例脚本，SKILL.md 阶段 4 分发原则）：只校验各实例 dp_size 相同，**不与本机实例数比较**——本机实例数（如 2）小于全局 dp_size（如 20）属正常
  - ⚠️ 守恒 FAIL 时先确认执行口径：目录内实例数 ≠ dp_size 不代表配置错；把 prefill.dp_size 改成本机实例数反而正是 KV 寻址错配事故（本 skill 反复警告的高频错误）
- 无 Ray 残留：脚本 grep 无 `--distributed-executor-backend` / ray backend 旗标
- 参考实现：`references/l0_static_check.py`（对生成的脚本/配置直接做全套静态检查，不依赖 NPU 环境）；现场另一种等价形态是 stub 掉 vllm 可执行捕获实际命令行再按同口径检查，两种形态的检查项一致

### l0_static_check.py 调用口径

```
python references/l0_static_check.py <脚本目录> \
  [--expect-dp <全局实例数>]      # 全量目录执行时给（核对 dp_size==全局实例总数）；按主机分目录执行不给
  [--tp <目标TP>]                 # 显式核对；缺省按脚本实际 --tensor-parallel-size 做组内一致性检查
  [--group-size 4]                # DP 组大小（混合分组如 "4,2"）
  [--legacy-baseline <现场旧脚本>]  # KV 旧字段（engine_id/kv_port/lookup_rpc_port/decode 段）期望值从现场旧脚本解析
  [--env-file <env文件>]          # env 文件传参形态：脚本 source 的 env 文件放脚本同目录即自动解析；本地拿不到（如 /etc/machine-llm/topology.env）就下载到脚本目录或用该参数显式给，可重复
  [--instances-per-host 2]        # 每机实例数（按文件名自然排序分组）；或 --hosts h1=2,h2=2 显式分组
  [--rpc-port 16600] [--no-keep-legacy]
```

要点：

- **期望值全部显式来自现场**（目标 TP、legacy 基线、全局实例数、每机实例数），脚本不写死任何基线值
- **env 文件传参形态**（SKILL.md 阶段 4 要求保留）可直接校验：脚本内 `source` 的 env 文件放脚本同目录自动解析；解析不到的变量（`$VAR` / `<ARG:var>`）对应检查项降级为 SKIP 并提示补 env 文件，不假 FAIL
- **主机分组**：子目录/文件名 `host<N>` 模式、`--instances-per-host`、`--hosts` 任选其一；都无法判定时按单文件独立分组并输出告警（不再按"文件名首个数字 //2"猜每机 2 实例，避免 8 卡机 TP8×1 假 FAIL、16 卡机 TP4 漏检）
- 单个脚本解析异常只记一条 FAIL，不影响其余脚本逐项结果输出

## L1 单实例启动验证

- 按 DP 组序拉起：先启组内 rank 0（主节点）再启其余（rendezvous 依赖主节点）；同机双实例无顺序要求。nohup 后台 + 日志落盘（`nohup bash xxx.sh > xxx.log 2>&1 &`）
- 每实例探活：`curl -m 5 http://127.0.0.1:<http_port>/v1/models`
- 日志确认：`tensor_parallel_size` 与目标一致（如 8）；`npu-smi info` 对照各实例占用卡与布局表一致（没绑错卡、没互踩）

## L2 DP 组网验证

- 组内 rank 0/1/2/… 全部入组（日志确认，无掉队/重试报错）
- 16600 组内互通：`ss -ltn` 确认主节点监听；组内每台 `curl/nc` 探通

## L3 KV 链路验证（Mooncake 对齐的唯一可靠判据）

- 请求必须经**指向新 P/D 的测试路由**发送（Proxy 加测试后端组或直连组合均以实际路由为准）：直连单个 P 实例拿到 token 不代表 D 参与了该请求——D 侧无报错可能只是"根本没用上"，不能作为 KV 链路通过的证据
- 用**同一请求标识**（request id）核对三段闭环：P 侧日志确认该请求的 KV 已发送、D 侧日志确认对应 KV 已接收、请求最终正常生成 token——三者对上才判 L3 通过，任一环缺失按失败处理
- 各 P 实例与 D 侧日志均无 Mooncake/DP 通信报错。`kv_load_failure_policy: fail` 下 KV 传输失败会直接报错——失败立即暴露而不是静默丢 KV；若报错，优先回头查 Mooncake tp/dp 对齐与新旧混跑

## 失败处理

L0-L2 失败 = 配置/环境问题，修复后从 L1 重来；L3 失败优先怀疑 Mooncake 错配或新旧混跑。测试结果逐层记录进部署指南。

L4-L6 交付验证不在本 skill 范围（转 `kptools-tpsplit-optimization-verify`）：L4 功能 / L5 性能对比 / L6 稳定性观察由 verify 执行，其 L5 消费的旧基线数据在阶段 3 停服前采样归档（见 SKILL.md 阶段 3 红线）。
