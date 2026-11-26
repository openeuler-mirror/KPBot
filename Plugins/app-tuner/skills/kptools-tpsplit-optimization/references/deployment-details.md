# 部署阶段细则 — kptools-tpsplit-optimization 阶段 1/3/4/6 展开

承接 SKILL.md 各阶段的压缩正文，保留完整表格与命令。SKILL.md 是主流程与红线，本文是展开细节，执行到对应阶段时读取。

## 1.3 配置调用链梳理：《配置修改点清单》模板

SSH 只读追完调用链（见 SKILL.md 1.3 的 grep 命令组）后产出下表，与用户确认后才进阶段 2：

| 要改的参数 | 真正定义的文件 | 消费方 | 改这一处是否覆盖全部消费方 |
| --- | --- | --- | --- |
| p_dp_size | （示例）/etc/machine-llm/topology.env | P 全部实例脚本 + D 侧脚本（同一 source） | 是→改 env 文件即可；否→逐文件列出 |

各形态的追踪要点：

- **systemd 场景**：`systemctl list-units | grep -i vllm` 找到 unit，`systemctl cat <unit>` 看 `Environment=` / `EnvironmentFile=` 指向的文件
- **容器场景**：`docker inspect <容器> 2>/dev/null` 看 Env 与启动 cmd（注意启动 cmd 可能又包了一层 shell 脚本）
- **父级编排场景**：`ps -ef | grep vllm` 反查父进程链，确认是谁拉起（shell / systemd / 容器 / supervisor / 驱动脚本），驱动脚本（如 launch_online_dp.py + 位置参数模板）的场景改模板与传参、不改驱动
- 追不清时（多层 source / 动态生成）把已知链路画给用户指认，不要猜

## 阶段 3 处置细则

- **旧集群处置**：阶段 1 已问过要不要停，此处执行。停 P 侧旧实例：`ps -ef | grep vllm` 找到 PID 逐个 kill（**勿用宽匹配 pkill**，会误杀其他进程）；旧方案确实用 Ray 时的 Ray 清理：先 `ps -ef | grep ray` **把每个 Ray 进程的归属核对到本次获准停服的旧集群**（同一部署用户、命令行/启动脚本指向本集群、启动时间与旧集群一致），确认后按 PID 逐个 kill 这些目标进程——**共享主机上禁用 `ray stop`**：该命令作用于本机全部 Ray 进程，会把其他业务/作业的 Ray 一起停掉（见 [Ray CLI 文档](https://docs.ray.io/en/latest/cluster/cli.html#ray-stop)）；仅当逐个核实本机所有 Ray 进程都属于本集群且已获准停服时，才允许 `ray stop` 兜底。停完复查 `ps -ef | grep -E 'vllm|ray'`：目标进程干净、端口已释放即通过；**其他业务的进程不在复查范围内，不得当成"未清干净"处理**
- **去 Ray 残留（仅旧方案有 Ray 时）**：检查旧启动脚本与用户 shell 环境，确认无 `--distributed-executor-backend ray`、无 Ray 相关环境变量（RAY_ADDRESS 等），新脚本中不引入。旧方案本来就没有 Ray 时这步无事可做，直接过
- **防火墙/安全组**：DP 组跨主机时，组内所有主机对 16600 互通；Proxy 到各实例 http 端口可达
- **资源占用协调（拉起前置条件）**：预检发现卡被其他业务/容器占用（`npu-smi` 有占用但不是本集群旧实例），或 kv_port / mooncake_master 等端口被外部业务占用时——**并入"拉起前置条件"清单交用户协调**，本 skill 不静默处理。注意跨业务并存时冲突不止是卡：http / kv_port / mooncake_master 端口同样会撞（端口三连占），只停卡不够
- 预检发现端口被占、卡数对不上、旧进程清不掉，立即反馈用户并停下，不要自行换端口/换卡继续

## 阶段 4 占位符与数据来源表

基于 `references/templates/prefill-tp8-instance.sh`（新方案占位符模板）为每个 P 实例生成启动脚本：

| 占位符 | 来源 |
| --- | --- |
| `<RANK>`（--data-parallel-rank） | 实例布局表 DP rank |
| `<DP_GROUP_SIZE>`（--data-parallel-size） | DP 组大小（组内所有实例一致） |
| `<DP_MASTER_IP>`（--data-parallel-address） | DP 组主节点 IP（组内一致） |
| `<DEVICES>` | 实例布局表 ASCEND_RT_VISIBLE_DEVICES |
| `<HTTP_PORT>` | 实例布局表 |
| `<P_TP_SIZE>` / `<P_DP_SIZE>` | 拓扑参数（P_DP_SIZE 全局值，所有实例一致） |
| `<D_DP_SIZE>` / `<D_TP_SIZE>` | D 侧现状（从旧脚本继承） |
| 模型路径 / served-model-name / max-model-len 等 | 旧脚本继承 + 2.3 用户确认结果 |

生成要点（与 SKILL.md 红线呼应的完整版）：

- 新脚本**不写 `--distributed-executor-backend`**（TP 单机闭环用默认后端即可）。旧脚本带 ray backend 的，生成时删除；旧脚本本来就没有的，保持没有——新方案任何情况下都不引入该旗标
- 每实例单独一份脚本（**独立脚本形态下**；驱动形态则是改模板+传参，验证口径相同）：实例间差异项只有 `--data-parallel-rank` / `ASCEND_RT_VISIBLE_DEVICES` / `--port` 三处（组级差异：DP 主节点 IP / 组大小），其余参数全组一致——生成后 diff 一遍，差异项之外的改动都是笔误
- DP 组网参数口径（对齐 vLLM 官方旗标）：`--data-parallel-size`（组大小）、`--data-parallel-rank`（本实例 rank）、`--data-parallel-address`（组主节点 IP）、`--data-parallel-rpc-port 16600`（组内统一）。注意 `--data-parallel-rank` 外部 LB 模式适用于 **MoE 模型**（GLM 系列 `--enable-expert-parallel` 场景即 MoE）；现场 vLLM-Ascend 版本若旗标名不同，以 `vllm serve --help | grep -i data-parallel` 实际输出为准——**该命令在无 NPU 的裁剪容器裸 shell 中可能直接失败**（torch_npu 加载报错），此时兜底口径：grep vllm 源码 `vllm/engine/arg_utils.py`（源码仓或 site-packages 内）确认旗标存在
- **生成脚本保持现场现有的传参形态**：参数在脚本内定义的，生成脚本内定义（模板形态）；参数从 env 文件 source 进来的，生成脚本继续 source 同一文件、只改 env 文件里的值——**不要把调用链改造成自己的习惯形态**，那会让后续运维找不到参数在哪
- **KV_TRANSFER_CONFIG 只改 p_dp_size / p_tp_size 两个值**，engine_id / kv_port / lookup_rpc_port / decode 段一律原样保留；**D 侧同样只改 prefill 段的两个值**；两个值改在 1.3 梳理出的**源头定义处**（共享 env 文件就改 env 文件，脚本自包含才改脚本）；改前备份原文件。这一原则不依赖 KV connector 的具体结构——旧脚本无论用 MultiConnector 还是其他 connector，改动面都只是 prefill 段的 dp/tp 两个值，模板里的 MultiConnector 结构以现场旧脚本实际结构为准
- **旧脚本与配置一律备份后再改**（同目录 `*.bak.<日期>` 或 backup/ 子目录，禁止原地覆盖删除）——回滚靠的就是这些备份
- 分发：每台主机只放自己的实例脚本；上传用 ssh-mcp-server upload 或 scp
- 远端自检通过才进阶段 5：`bash -n <脚本>` 逐个过；KV_TRANSFER_CONFIG 是嵌在脚本里的字符串，用一段 python 从脚本中抽出该变量并 `json.loads` 校验合法性

## 阶段 6 交付细则

1. **Proxy 更新**：L1-L3 全绿后才切流量——把 P 后端列表换成新实例地址（`<主机>:9000`、`<主机>:9001` …），按用户网关类型（MindIE Motor / Nginx / 自研等）改其配置文件并 reload（改前备份，改后探活）。MindIE Motor 类网关通过其 `register.sh` 重新注册新实例——注册表是内存态，coordinator 重启或后端换血后必须重新执行注册，否则请求报 No instance available
2. **产出部署指南 `.md`**（用户指定目录或部署目录），内容：拓扑信息摘要（布局表 + 修改点清单 + Mooncake 参数）、启动顺序与命令、L0-L3 测试结果记录、回滚步骤（恢复备份的旧脚本/配置 + 按旧方案原样重启，命令写完整）、停服前归档的旧基线数据（负载/并发/计时口径，供 kptools-tpsplit-optimization-verify（配套 PR #441 交付） 的 L5 对比）；指南中注明 L4-L6 交付验证由 kptools-tpsplit-optimization-verify（配套 PR #441 交付） 执行并回填结果
