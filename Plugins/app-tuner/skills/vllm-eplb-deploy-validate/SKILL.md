---
name: vllm-eplb-deploy-validate
description: 把 EPLB 参数方案确定性落到 vLLM P 节点启动模板，并组织 off/dynamic/force 三态对照压测、采集原始数据。当用户说"帮我把 run_dp_template.sh 改成 dynamic/force/off""切换/还原 EPLB 配置""EPLB 参数没独占一行""合并 additional-config 的 enable_force_eplb""造一份不均衡数据集""跑 EPLB 的 off/dynamic/force 对比"时，使用本技能。只负责部署与采集，收益结论可后续单独分析；是否真的实测由使用者决定，实测需提供 benchmark 服务器。
license: MulanPSL-2.0
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: vllm_moe_eplb
  activation_mode: active
  side_effect_free: "false"
  version: "1.0.0"
---

# EPLB 部署与收益验证（vLLM MoE）

本技能可**独立使用**：把显式的 mode 与参数（可选：读取一份参数方案）**确定性写进 P 节点模板**，
造不均衡数据集，组织 off/dynamic/force 三组对照并生成运行清单。它**不依赖任何尚未合入的技能**；
收益结论可后续单独分析。

```
输入：显式 mode + 参数（可选 eplb_parameter_plan.json）--> 本技能 --> 模板 + eplb_run_manifest.json + measurements
```

## 输入契约

`eplb_parameter_plan.json`：`mode(dynamic/force/off/hold)`、`ep_size`、`redundant_experts`、
`window_size`、`step_interval`、`template_changes`。`mode=hold` 时不要落盘。

现场：P 节点模板路径、是否本站点分支、压测计划（数据集、并发、轮次、benchmark 服务器/端点）。
**是否实测由使用者决定**；若实测，必须先拿到可用的 benchmark 服务器与 P/D/代理端点，缺就不跑、只落盘与交命令。

## 三种状态（互斥）

| 状态 | V2 runner | 环境变量 | serve 参数 | 定位 |
|---|---|---|---|---|
| `off` | `=1`（仅当模板原本开过 EPLB，否则不改 V2） | 无 `DYNAMIC_EPLB` | 无 eplb 参数 | 基线，用来做对照 |
| `dynamic` | `=1` | `export DYNAMIC_EPLB="true"` | `--enable-eplb` + 3 个 `--eplb-config.*` | **优化手段，唯一可上线形态** |
| `force` | `=0` | `unset DYNAMIC_EPLB` | 在 `--additional-config` JSON 里加 `"enable_force_eplb":true` | 均衡性能的**参照上限**，不可上线 |

`force` 不解决不均衡，它是把"不均衡"从测量里拿掉。永远不要把 force 当作交付配置。

> 目标环境：本站点内部 vLLM 发行分支。`DYNAMIC_EPLB` 与 `enable_force_eplb` 是该分支的开关，
> 上游 vLLM 不含这两个名字，照搬会静默无效。换环境前先确认分支。

## 硬性约束（脚本已内建）

1. **只改 P（prefill）节点**的 `run_dp_template.sh`。D 节点/代理/mooncake 脚本一律不动；正向判据是
   `--kv-transfer-config` 的 JSON 里 `"kv_role":"kv_producer"`。
2. `dynamic` 依赖 `VLLM_USE_V2_MODEL_RUNNER=1`，`force` 依赖 `=0`，两者互斥，只改一半等于配置矛盾。
3. 已有 `--additional-config` 时必须**合并进那个 JSON**，不能出现第二个同名 flag。
4. 关闭 dynamic 用 `unset DYNAMIC_EPLB`（unset 不接受 `=`）。
5. 改文件前留带时间戳的备份；写入用同目录唯一临时文件 + 原子替换。
6. **不代重启服务**；重启命令交给用户按现场 IP/目录执行。
7. **只写用户指定的那个模板**：不读凭据、不访问网络、不改其他文件。

## 怎么部署：用脚本，不要手写 sed

```bash
python3 <skill>/scripts/apply_eplb.py --template <P节点 run_dp_template.sh> --mode check     # 只读诊断
python3 <skill>/scripts/apply_eplb.py --template <...> --mode off                            # 还原基线
python3 <skill>/scripts/apply_eplb.py --template <...> --mode force --dry-run                 # 看 diff 不写
python3 <skill>/scripts/apply_eplb.py --template <...> --mode dynamic --ep-size 16 \
    --redundant-experts 16 --window-size 1000 --step-interval 1000                            # 按 plan 落盘
```

脚本保证：只接受 `kv_role=kv_producer` 的 P 节点模板；拒绝没有 `vllm serve` 的脚本；没有
`--enable-expert-parallel` 时拒绝开 dynamic；EPLB 参数与其它参数写在同一行时拒绝（布尔开关
`--eplb-config.log_balancedness` 后跟另一 flag 也算同行）；`dynamic` 必须给 `--ep-size` 并据其校验
整除、非整倍向上取整；`--mode off` 只按有效（非注释）配置判断模板是否开过 EPLB；`--enable-expert-parallel`
是 serve 末参数时，EPLB 参数插在该行之前、不改 EP 行本身，重复执行或切 off/force 都不会留下悬空续行符
把下一行命令吞进 serve（verify 会检测并拒绝）；符号链接先解析真实文件；写后 `bash -n` 失败自动回滚；
重复执行同一模式幂等。

> 同一组 flag 不要既按 analysis 清单手改、又用本脚本加一遍——会让模板自相矛盾。

## 造不均衡数据集

混合在线流量接近均衡，EPLB 大概率变慢，结论会被误读成"负优化"。验证要用把不均衡放大到极致的流量：

```bash
python3 <skill>/scripts/gen_imbalanced_dataset.py --out imbalanced_100x100k.jsonl \
    --num-requests 100 --target-tokens 100000 --model dsv41
```

每条请求带约 100k token 的重复长前缀，专家命中高度集中。输出 dumpjsonl（每行一个 OpenAI chat 请求），
配 `benchmark_serving.py --dataset-name dumpjsonl` 使用。

## 三组对照（是否实测由使用者决定）

| 组 | 配置 | 回答什么问题 |
|---|---|---|
| 基线 | `off` | 起点 |
| 优化 | `dynamic` | 实际能拿到多少 |
| 上限 | `force` | 均衡后还能再多多少 |

每组跑 4 轮；**开 EPLB 后第 1 轮是预热离群值**（首次专家统计与重平衡），剔首轮取后 3 轮。组间切态前先
`--mode check` 确认落到目标态，避免在混合态上压测。

写入运行清单，把"部署了什么"和"怎么测的"固定下来，供收益分析消费：

```bash
python3 <skill>/scripts/make_run_manifest.py --out eplb_run_manifest.json \
    --template <P节点模板> --mode dynamic --ep-size 16 --redundant-experts 16 \
    --window-size 1000 --step-interval 1000 \
    --dataset imbalanced_100x100k.jsonl --concurrency 32 --rounds 4 \
    --service dsv41-prefill
# measurements: 每组每轮 TTFT（benchmark --save-result 结果，或归一化 JSON）
```

**实测前置**：使用者提供 benchmark 服务器与端点后才执行；Agent 不擅自重启服务、不擅自压测。若使用者
选择不实测，只交付"模板已落盘 + 数据集 + 命令 + manifest 模板"，把测量留空。

## 重启命令（交给用户执行）

```
# P 节点
docker exec -it <container> bash
cd <P脚本目录> && bash start_mooncake.sh && bash start_p0.sh
# D 节点
docker exec -it <container> bash
cd <D脚本目录> && bash start_d0.sh
# 代理（P 节点，端口 12800）
cd <P脚本目录> && bash start_proxy.sh
```

等 `log/prefill_*.log`、`log/decode_*.log` 显示就绪后再经代理发请求。

## 输出契约（供后续收益分析/人工使用）

- `eplb_run_manifest.json`：`template.sha256`、`params`、`experiment{dataset,concurrency,rounds,groups,drop_warmup_round}`。
- `eplb_measurements.json`：三组逐轮原始 TTFT（`{group: [ttft_round1, ...]}`），保留原始值不预先求均值。

## 排查（部署/模板类）

| 现象 | 处理 |
|---|---|
| `no vllm serve command found` | 模板不是 vLLM 启动脚本，或 serve 被别名/函数包了一层 |
| `no --enable-expert-parallel` | dynamic 以 EP 为锚点，先确认 EP 配置存在 |
| `no --additional-config block found`（force） | 模板缺该块；手工加一个（且只加一个）再跑 |
| force 下 `--additional-config` 数量不是 1 | 模板本来就有重复 flag，先合并 |
| 报"EPLB 参数没有独占一行" | 把 EPLB 参数拆成独立行，避免误删 `--port` 等相邻参数 |
| dynamic 写入参数与预期不符 | `--ep-size` 为 dynamic 必填；非整数倍会取整并提示 |
| 切 off 后 V1 模板被翻转 | 仅当模板有效配置里确有 EPLB 标记才动 V2；注释不算 |
| 写后 `bash -n` 失败 | 脚本自动回滚到备份，检查模板 shell 结构 |

收益是否达标、要不要保留/回退，可在拿到 measurements 后另行分析。
