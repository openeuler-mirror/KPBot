---
name: kptools-tpsplit-log-trace
description: >
  把 TTFT breakdown 打点 patch 部署到远端 vLLM Ascend 容器（PD 分离）并校验生效。当用户提到
  以下场景时必须使用此技能：部署打点/插桩、TTFT patch、deploy_ttft_full.sh、docker cp 覆盖
  vllm 源码、部署后重启引擎、md5 基座核对、VLLM_TTFT_SHM_LOG、给 P/D 容器推打点、
  [TTFT][BREAKDOWN] / [SHM_DEQUEUE] / [TTFT_KVXFER] / [LB_DISPATCH] / [TTFT_DRAFT] 打点部署、
  打点部署后不生效排查。即使用户只说"把打点推上去"或"更新一下 TTFT patch"，也应使用此技能。
  只负责部署，不含日志解析出表（出表见 kptools-tpsplit-log-analysis skill）。
license: Proprietary
compatibility: 本地 Windows（patch 包在 D:\workspace\vllm_ascend\patched_src，写出的 .sh 需去 CR）；SSH 访问 P/D 宿主机（使用调用方已授权的目标连接；认证失败即停止，请维护者更新该连接，不绕行其他主机）；远端 docker（stopped 容器只能 docker cp，docker exec 无效）；容器内 /workspace/vllm 源码根 + site-packages 下的 vllm_ascend；连接方式优先 ssh-mcp-server 的 upload/execute-command/download，无 MCP 时回退本地 ssh/scp
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: ttft_log_instrumentation
  version: "1.0.0"
---

# kptools-tpsplit-log-trace：TTFT 打点部署（日志插桩）

把本地已做好的「TTFT breakdown / KVXFER / SHM_DEQUEUE / LB_DISPATCH / DRAFT 打点」部署到远端多台 vLLM Ascend 容器并校验生效。**只部署，不解析日志；出表见 kptools-tpsplit-log-analysis skill。**

## 快速决策

1. 用户要部署打点（默认场景）→ 走第 3 节完整流程：确认参数 → 上传 → 输入预检（存在/known-good 指纹/py_compile）→ 备份全部目标 → md5 基座核对 → 覆盖 → grep 校验 → 提示用户重启 + smoke
2. 用户只要部署脚本（远端自己跑）→ 只产出 `deploy_ttft_full.sh` + 使用说明，不连远端
3. md5 / 基座指纹对不上 → **立即停下问用户**，禁止强推（见第 5 节踩坑）
4. 部署后打点没输出 → 先查第 4 节环境变量与 Ray 日志（VLLM_TTFT_SHM_LOG / Ray actor env / RAY_DEDUP_LOGS），再按第 3 节第 6 步 grep 校验部署是否生效
5. 用户要解析日志出 TTFT 拆解表 → 不归本 skill 管，转 kptools-tpsplit-log-analysis

## 1. 集群与拓扑（部署前核对）

PD 分离：P 做 prefill、D 做 decode，proxy 是宿主机进程。

| 集群 | P 节点 | D 节点 | 备注 |
|---|---|---|---|
| 105/106/107 | 105+107（Ray TP32，head=105） | 106 | proxy 跑 106 |
| 13/11/102 | 13+11（`vllm-ascend-glm52-p-gt`，目录 `p0-1112`/`p0`） | 102（`vllm-ascend-glm52-d-gt`，`d0-1112`） | proxy 在 102 宿主机 7200 |
| 102/103/104 | 102+104（glm52-p） | 103（glm52-d） | 102/103 常 stopped |

- 连接一律使用调用方已授权的目标 SSH 连接；认证失败即停止，由维护者更新该连接后重试，不绕行其他主机。
- 容器内 vllm 源码根 = `/workspace/vllm`（PYTHONPATH 注入）；vllm_ascend 在 site-packages（SP=`/usr/local/python3.12.13/lib/python3.12/site-packages`）。`/data` 是共享挂载，容器内可见宿主机文件。
- ⚠️ 同机常有他人容器（dsv41/v30 引擎）占 NPU，**勿停勿改**；起集群前先看 NPU 占用。

## 2. patch 包清单（`D:\workspace\vllm_ascend\patched_src`）

| 文件 | 关键打点 |
|---|---|
| `vllm/v1/metrics/stats.py` | `[TTFT][BREAKDOWN]` v3 行格式 + 主守恒式 |
| `vllm/v1/engine/__init__.py` | frontend_ts / lb_dispatch_time / schedule_total_time（必填字段须在可选字段前） |
| `vllm/v1/engine/core.py` | TTFT_DECODE、`_ttft_stamp_deq`（P11） |
| `vllm/v1/engine/input_processor.py` | client_send_ts / `_get_lb_dispatch` |
| `vllm/v1/outputs.py` | `worker_fwd_pure_time` |
| `vllm/v1/request.py` | ttft_* 累计字段（含 inputq_wait / add_req_proc） |
| `vllm/v1/core/sched/output.py` + `scheduler.py` | schedule_total_time、整批墙钟、add_req 拆分 |
| `vllm/v1/worker/gpu_model_runner.py` | 纯前向钩子 + `[TTFT_DRAFT]` |
| `vllm/v1/worker/kv_connector_model_runner_mixin.py` | worker load/forward/save 计时 |
| `vllm/distributed/device_communicators/shm_broadcast.py` | `[SHM_DEQUEUE]` / `[SHM_DEQUEUE_BATCH]`（懒读 env） |
| `vllm_ascend/core/recompute_scheduler.py`（→SP） | D 侧整批墙钟 |
| `vllm_ascend/worker/model_runner_v1.py`（→SP） | **v3.1 forward 钩子+收割、v3.2 draft 闭包**（Ascend 双 override，必须补） |
| `pool_worker.py` / `ascend_store_connector.py` / `kv_transfer.py`（→SP ascend_store） | `start_load_kv cost time`、`[TTFT_KVXFER][put]` |
| `proxy/load_balance_proxy_server_example.py` | `[LB_DISPATCH]` + client_send_ts 透传（宿主机进程） |
| `utils/bench_serving/backend_request_func.py` + `benchmark_serving.py` | 注入 client_send_ts、`--save-detailed`（宿主机进程） |

**版本指纹（known-good，2026-11-18 v3.2）**：16 文件全量登记于脚本 `PATCH_FP`——写入前比对上传件、写入后回读比对，登记不全脚本拒绝运行（拒绝绕过内容核验）；宿主机侧 proxy/压测端 3 文件登记于脚本 `HOST_FP`，同一门禁（写入前 md5 前缀比对 + py_compile、写入后回读 md5 并核对指纹）。易变重点文件：`model_runner_v1=2e7db5f3`、`shm_broadcast=55821b06`、`gpu_model_runner=0a59cde4`、`stats=9f9f5276`、`sched_output=c6d9b049`。

## 3. 部署流程

1. **确认参数**：P/D 宿主机+容器名、容器内源码根、本地 patched_src、是否连 proxy/压测端一起推（`PROXY_TGT` / `BENCH_TGT`）。
2. **上传**：`scp -r patched_src root@<宿主>:/data/<user>/`，部署脚本一并拷到 `scripts/`。
3. **备份 + 基座核对（必须）**：排他创建本轮私有备份目录（宿主机 `mktemp -d /tmp/ttft_bak.<容器名>.XXXXXX`，并发部署 P/D 互不覆盖），**按原路径备份本轮全部目标**（vllm/SP/KVXFER/proxy/bench，写 MANIFEST 映射，区分新文件与读取失败，备份不完整即停）；基座指纹不可读或不符即停，**禁止强推**（防覆盖他人特有改动）。
4. **覆盖**：共享挂载下容器内直接 `cp`；否则宿主机 `docker cp`（对 **stopped 容器同样有效**，`docker exec` 无效）。SP 文件先用 `find` 定位：

   ```bash
   find / -path '*vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store' -type d
   find / -path '*site-packages/vllm_ascend/core/recompute_scheduler.py'
   ```

5. **一键脚本**：`bash scripts/deploy_ttft_full.sh <CONTAINER> [SRC] [KVXFER]` —— 输入预检（文件存在 + **16 文件全量 known-good 指纹比对** + py_compile，写入前完成、登记不全即拒绝运行；设 `PROXY_TGT`/`BENCH_TGT` 时宿主机 3 文件走 `HOST_FP` 同一门禁）→ 定位 SP/KVXFER（find 不限深度、prune /proc /sys，SP/KVXFER 探测失败即停，不做未验证路径写盘）→ 按原路径备份全部目标 → 基座指纹核对（不可读即停）→ 现役 vs 目标 md5（**vllm/SP/KVXFER 全量，未知摘要即停**，人工确认后 `ALLOW_UNKNOWN_DIGEST=1` 放行）→ docker cp 16 文件并回读校验 → proxy/压测端（可选，`HOST_FP` 指纹门禁，失败即停 + 回读 md5 比对）→ grep 校验（含跳过写入的现役文件）→ 提示重启。
6. **校验**：容器内 grep 各关键字段应 >0：`frontend_ts`、`schedule_total_time`、`queue_wait`、`step_other`、`add_req_iq`、`TTFT_DECODE`、`TTFT_KVXFER`、`start_load_kv`、`TTFT_DRAFT`、`_ttft_in_draft`、`_TTFT_SHM_DQ`；再与本地逐字节 `md5sum` 比对。
7. **不自动重启**：由用户停启 P/D 引擎与 proxy。顺序参考：pkill launch_online_dp / vllm serve / EngineCore → P 起 mooncake_master → P `start_p0.sh` → D `start_d0.sh` → 102 宿主机 `start_proxy.sh`。
8. **smoke**：发 1 条请求，确认 `[TTFT][BREAKDOWN]` v3 行、`[TTFT_DRAFT]`、`[SHM_DEQUEUE_BATCH]` 有输出、端口监听、`Application startup complete`。

## 4. 启动环境变量与拓扑

- P/D 引擎启动 shell 必须 `export VLLM_TTFT_SHM_LOG=1`（shm dequeue 归因总开关）；`VLLM_TTFT_SHM_BATCH=1` 默认开。
- **Ray executor**：worker 是 Ray actor，env 来自 raylet，启动 shell 的 export 不生效 → patch 已改 dequeue 内懒读 `os.environ`。Ray 远端日志经 log_to_driver 汇总到 head 一份日志，需 `RAY_DEDUP_LOGS=0`。
- 拓扑：P=dp×tp（端口按 dp 数给，如 dp2 挂 9900/9901），D=dp8×tp2（9100-9107）；proxy 的 `--prefiller-ports` 必须匹配 P 的 dp 数，否则流量全进 dp-rank0。

## 5. 部署踩坑（务必遵守）

- **禁止携带未运行验证的改动**：P10 曾把未验证的 `shm_broadcast.py`（dequeue finally 内累计 `_obj`）全量推送 → D 侧 `get_kv_cache_specs` RPC 返回类型损坏、EngineCore 启动即崩；回滚 55821b06 才恢复。**清单内每个文件 md5 必须与已验证版本一致。**
- **先核验后写入**：输入 known-good 指纹与 py_compile 必须在任何 docker cp 之前完成——先覆盖后校验会把被拒绝的内容留在目标目录。
- **现役摘要未知即停**：现役文件既非目标版本也非已知打点版本时，可能含现场定制；列出差异并停止，人工确认后以 `ALLOW_UNKNOWN_DIGEST=1` 显式放行。
- **部署 ≠ 运行**：部署完必须重启 + smoke 才算闭环。
- **基座指纹**（patch 未触碰文件）：`detokenizer.py=3fe8bf88`、`kv_cache_manager.py=d1a6c67c`、`version.py=2b4efcd3`；不符=基座不同，停下确认。
- `EngineCoreRequest` 必填字段（lora_request/cache_salt/data_parallel_rank）必须在可选字段之前，否则启动崩。
- Ascend 用 v1 runner（`VLLM_VERSION=0.23.0` 强制回落）：forward/draft 钩子必须落在 `vllm_ascend/worker/model_runner_v1.py` 的 override 里，**钩子 + 收割都要补**，只补收割点仍恒 0。
- `defer_finalize=True` 下 producer 侧 `save_kv` 走 sentinel 恒 0、engine 侧 `load_kv` 恒 0 —— 属已知设计，真实值在 pool_worker / kv_transfer 日志里（解析层回填）。
- Windows 写出的 .sh 带 CR：远端先 `tr -d '\r' < f > f2`。
- 宿主机上 python3.12.16 进程不是我们的引擎（我们是 3.12.13）；用 `/proc/<pid>/cgroup` + `readlink /proc/<pid>/cwd` 判断归属。
