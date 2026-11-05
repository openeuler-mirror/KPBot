---
name: ascend-vllm-profiler
description: |
  分析 torch_npu/Ascend profiler 的 trace_view.json（LLM/vLLM 推理场景）。
  能力：① 任意时间段或任意函数时段的 Computing/Communication/Free 精确占比
  ② 函数定位、消歧与跨层调用链重建 ③ host bound 归因分析
  ④ 版本源码对照解读（vllm-src 子 skill）。
  当用户提供 profiling 数据、询问某函数/时间段占比、free 过高原因、host bound、
  卡间通信等待时使用。
---

# Ascend vLLM Profiler 分析

## 输入与产物

- 输入：`trace_view.json`（torch_npu profiler 导出，chrome trace 格式，单行大文件）；同目录侧车 `communication.json`/`api_statistic.csv` 若存在则一并入索引
- 产物：sqlite 索引（`~/.cache/ascend_prof_skill/`，一份 trace 只建一次，5s；零点自动取全事件最早 ts，仅手动 calibrate 过的重建才继承；meta 含各层 pid（python/device/cann/oa）、coord_streams 协调流、has_stack、ui_zero_source）
- 源码对照：`vllm-src/` 子 skill（版本知识库 + repos/vllm、repos/vllm-ascend 浅克隆）

## 工作流（铁律，按序执行）

1. **建索引**（若该 trace 无索引）：
   `python3 scripts/build_index.py <trace_view.json>`
2. **时间基准**（build_index 已自动校准，一般无需用户提供信息）：
   自动零点 = **全事件（含 metadata）最早 ts** = chrome trace 查看器（MindStudio Insight）零点——比最早 X 事件早几十 ms（8B 实测 75ms），这就是旧版"工具零点≠trace最早事件"的根源。
   仅当报告时间与用户工具显示仍不一致时才手动校准：让用户读任一熟悉函数的 `start 显示值` 和 `start(raw timestamp)`，
   然后 `python3 scripts/query.py <db> calibrate <函数子串> <UI显示ms> --raw_ns <raw ns>`（标记 manual，重建继承）。
3. **函数锚定查询**（用户问"某函数时段占比"时）：
   - 先 `find <子串>` 列出全部候选（同名函数可能有多层/多调用点）
   - 多候选时**不要猜**：列出候选的开始时间(UI)让用户指认；或按启发式（见知识库）选择并说明
   - 再 `span <函数名> <UI开始ms>` 得四分类占比
   - `chain <函数名> <UI开始ms>` 查外层调用链（同线程真调用链 + 跨层 CANN 实现，分列展示）
4. **任意窗口**：`window <UI起ms> <UI止ms>`
5. **逐实例模式**（用户问"每个 xx 函数执行时段"时）：
   `span <函数名> --in_step <N>` → 该 step 内全部实例逐个输出占比 + min/avg/max 汇总
6. **多 step 汇总 / host bound 判定**：`report [--step N]`——自动定位每步三阶段窗口（锚点=`_model_forward` 优先，整图回放步也存在；缺失才退化 `_execute_model_orig` 并标注），每步输出 P1/forward/P3 耗时+四分类、forward 执行模式（整图回放/FX eager）、Preparing、end_sync、设备同步等待（多流合计可超墙钟）、判定（host bound/设备积压/forward 供数不足）。
7. **通信明细**：`comm [--step N] [--limit 20]`——communication.json 通信算子逐个耗时/等待/transit（wait 占比高=rank 间不均衡或同步设计），step 汇总 + CANN Total 行交叉核对。
8. **API 聚合**：`api [子串] [--level acl] [--limit N]`——api_statistic.csv 聚合（host 侧 API 耗时排行）；`api aclrtSynchronize` 的 Device 同步总耗时应与 report 的 end_sync 合计对上（±0.5%）。
9. **HTML 报告**：`report_html.py <db> [--out report.html] [--step N]`——单文件可交互报告，约定见下节。
10. 输出时间一律 **双基准**：`UI xxx.xxxms (abs ...ns)`，时长用 ms（查询/对话输出；M2 报告内除外，见报告格式）。
11. **四分类**：computing / communication / **communication(not overlapped)** / free 全部输出
    （用户问占比时默认给全四个；commNO ⊆ communication，两者差 = 被计算重叠的部分）

## trace 结构知识（pid 动态，勿硬编码）

各层 pid **随环境/rank 变化**（27B 实测：176/180479/180672/180736/181216；8B 实测：149/152831/153088/153568），按内容识别，build_index 自动判定并写入 meta（`python_pid/device_pid/cann_pid/oa_pid`），脚本一律从 meta 读（旧 DB 无该键时回退 27B 旧常量）：

| 层 | 内容 | 识别规则 |
|-----|------|------|
| python_pid | python_function 栈 + cpu_op(aten/vllm) + enqueue/dequeue + ProfilerStep# | 有 cat=python_function 事件的 pid |
| cann_pid | CANN host API（`AscendCL@aclrt*`）| name 以 `AscendCL@` 开头 |
| device_pid | 设备执行事件（tid=stream，cat/args 含 Task Type: AI_CORE 等）| cat/TaskType ∈ AI_TYPES |
| （通信机制事件）| Notify_Wait/Notify_Record/Inter_Rank_Record/Memcpy 等，与 hcom 配套 | 独立 pid，cat 为空 |
| oa_pid | **Overlap Analysis**（name=Computing/Communication/Communication(Not Overlapped)/Free）| 按 name 识别（任意 pid），权威分类源 |

## 分类语义（CANN step_trace_time.csv 对齐：with-stack ±0.05%，without-stack 三分类 0.00%）

- **窗口**：函数锚定问题用函数实例的 [start, start+dur]；step 问题用 ProfilerStep#N
- **Computing**：AI 类任务覆盖（AI_CORE/AI_VECTOR_CORE/MIX_*/AI_CPU/DSA_SQE），**排除 HCCL 协调 stream**（识别规则：某 stream 的 AI 任务 100% 是 AI_CPU 且数量大——它们与 hcom 内核一一对应，是通信机制一部分）
- **Communication**：`hcom*` 内核覆盖
- **Free = 窗口 − (Computing ∪ Communication)**：**必须按补集算，不能直接加 OA 的 free 区间**——Overlap Analysis 不覆盖窗口边缘（步首 ~1-2.3ms），小窗口（如 2.5ms 的 P1-B）直接加 OA free 会只剩 6% 而 94% 凭空消失（2026-09-29 修复）；此口径下 **Computing + 未重叠 + Free = 100%**（Communication 与 Computing 可重叠，是超集信息）
- **Preparing** = step 开头首个 free 区间（到首个 AI/通信内核）
- 分类源优先用 pid 181216 的 Overlap Analysis 事件；缺失时 build_index 自动自建重建（结果与 CANN 一致）
- 无 Overlap Analysis 的旧数据/无栈数据同样适用自建路径

## vLLM 常见函数识别（消歧启发式）

- **P2 模型前向锚点铁律**：优先 `model_runner_v1.py: _model_forward`（eager / FX eager / **整图回放步都存在**）。模型级 `qwen3_5.py(626): forward` 只在非回放步存在——**整图回放步没有模型级 forward python 帧**（回放不进 python），用旧锚点会误回退到 `_execute_model_orig`（窗口混入 P1）。`_model_forward` 与模型级 forward 在 eager 步几乎重合（~30µs）
- **执行模式三分**（P2 语义判别，源码依据见 vllm-src/knowledge）：
  - **整图回放**：窗口内有 `torch_npu/npu/graphs.py(638): replay` → 纯（均匀）decode 步：每请求恰好 `uniform_decode_query_len`（无 spec=1，有 spec=1+draft数）个 token 且全部 prefill 完（spec decode 下还要求 is_all_decode）→ FULL 模式。host 仅 ~3ms 下发 + `_update_full_graph_params_if_needed`（update_stream 上 graph_task_update_begin/end ×N 更新 KV 地址等）；设备异步执行整图，P3 期间排空，步尾同步长（真排水）。**图捕获发生在 profiler 启动前（引擎初始化 dummy run），trace 不可见**
  - **FX eager**：窗口内有 `fx_run_eagerly`（torch_npu/dynamo/npugraph_ex/.../acl_graph.py:895）→ torch.compile FX 图**逐算子 eager 执行，不是回放**；批内含 prefill 请求的步专属（混合批或纯 prefill，非均匀 decode）。所有步的 `self.model` 都是 ACLGraphWrapper（`vllm_ascend/compilation/acl_graph.py(138): __call__` 每步必现）：模式不匹配时直接内联执行 runnable（其时长=整段 forward），模式匹配时 replay
  - MTP draft 三轮 forward 在所有步都走 FX eager（不走整图回放）
- **批成分判定（混合批 vs 纯 decode，2026-09-29 实测；2026-10-16 修正 decode 口径）**：vLLM v1 是 continuous batching，**一个 step 的批可同时含 prefill 与 decode（混合批），不是二选一**。从 trace 判定：① 批内请求数 = `update_req_spec_token_ids` 调用数（源码 gpu_model_runner：存量请求循环与新请求循环各调一次）；② **decode = 批内 − 新增 add_request**（存量请求，**带不带 draft 都是 decode**——旧表把"decode（带 draft）"当 decode 列，无 spec decode 模型恒 0，与"新增 prefill=0"并列会被读成"批内没有请求"，8B 需求方反馈实测教训）；③ 其中带draft = 慢路径（≥6µs，写 token_ids_cpu）计数，**前提 trace 全程含 `propose_draft` 帧**（无 = 该模型无 spec decode，恒 0；step0 首次调用 warmup 可超阈值是假阳性）；④ 新增 prefill = `add_request` 数，**必须限定 `gpu_input_batch.py%: add_request`**（logits processor `builtin.py(191): add_request` 同名重名陷阱，27B 实测会×2）；⑤ prefill 存在性通用信号 = `split_decodes_and_prefills`（vllm-ascend attention/utils.py:337，各注意力后端 build 必调）：纯 decode 早退 ~20µs **无 cpu_op 子算子**，含 prefill 走张量分析 130µs+ 且有子算子（8B/27B 实测）——new=0 但有 prefill = chunked prefill 续算步，勿判纯 decode；⑥ `_execute_mm_encoder` 真跑（ms 级）= 有新图像请求，µs 级 = 空调用。报告表格列：批内请求 | decode | 其中带draft | 新增 prefill | 视觉编码 | 批类型
- **forward** 多层同名：`qwen3_5.py(626): forward`（模型级，外层）→ `qwen3_next.py(505): forward`（核心）→ `<string>(19): forward`（FX eager 执行主体，每步多次）。问"forward"默认取模型级，但须列出候选说明。
- **synchronize** 多种：
  - `torch_npu/npu/utils.py(62): synchronize` → `_npu_synchronize` → `AscendCL@aclrtSynchronizeDeviceWithTimeout`：**设备级同步**，ms~几十 ms 级；步尾 end_step 内必现（见 P3-C）——瞬时返回（<0.1ms）说明设备早已空闲，是 host bound 信号
  - `torch_npu/npu/streams.py(183): synchronize` → `Event::synchronize` → `aclrtSynchronizeEvent`：event 级，微秒~亚 ms，input-prep 常见
  - `streams.py(79): synchronize`：Stream 级
  - `gpu_model_runner.py(3703/3714): synchronize_input_prep`：vLLM 调用点（内含上述 event sync），**用户问 synchronize 时不要答这个**
- 每步固定出现：ProfilerStep#N（步边界）、synchronize_input_prep ×2、handle_output ×2（P3，execute_model 后 + sample_tokens 后）；Event::synchronize 数量随步而变（step1 ×4，step3 ×0），**不是固定锚点**
- **注意**：`Event::synchronize` 是 cpu_op 层名，python_function 调用树中不出现——树里对应的是 `npu/streams.py(183): synchronize`
- 调用链跨 pid：python(176) → cpu_op(176) → CANN(180479)，chain 命令按时间包含关系重组

## M2 调用树规则（已与需求方校准，工具 scripts/calltree.py）

1. 树建到 **python_function 层**（host 侧），**全展开递归下钻**，具名函数（vllm/vllm_ascend/torch 序列化/numpy 等）不允许省略；关心函数的子函数继续挖到叶子
2. **cpu_op（aten::/vllm::）不进树**，但要找到其 python 调用入口进树（例：`vllm::all_reduce` → `distributed_c10d.py(2917): all_reduce`）
3. **可折叠机械层**（calltree.py 已内置）：`module.py(1780): _call_impl`、`torch/_ops.py: __call__`（op 分发）、`<built-in method`（tensor/ndarray 方法；`<built-in function` 保留，如 pickle.loads、_npu_synchronize）、`*_dispatcher`、`decorate_context`、profiler wrapper、logging 全内部（`logging/__init__.py`、`*logger.py`——c10d_logger wrapper 为纯透传，all_reduce 等子调用自动上提）、`typing.py: cast`、`gpu_input_batch.py: req_ids/num_reqs`（μs 级属性访问器）、`autograd/profiler.py`（记账）、`_has_torch_function*`、`torch/_tensor.py`（tensor 方法→aten 分发）、`_get_device_index`
4. 深度不固定：反序列化钻到 torch/storage 层、输入准备钻到 block_table 层、attention 钻到 gdn builder 层——**实现层即停**
5. 用法：`calltree.py <db> <UI起ms> <UI止ms> [--min-dur-us N] [--out file]`；展示可用阈值过滤，**完整文件必须 0 阈值**
6. 耗时不是筛选标准（0.018ms 的 repeat 也关心）；容器型函数（execute_model/_call_impl）不是关心对象
7. **同名兄弟聚合**（2026-09-29 增补，report_html 的 agg_siblings 已内置）：同一父节点下同名实例（如每请求一次的 `update_req_spec_token_ids` ×32、每层一次的 GDN 核心函数 ×48）合并一行——`函数 ×N · 合计 x ms · 单次 min~max ms`，子树保留最长实例的；火焰图**不聚合**（保留时间线真相）。逐实例分布用 `span <函数名> --in_step N`

## M2 报告格式（2026-09-23 与需求方校准）

**详细版（流程梳理，默认输出）**——按节输出，只写流程，**不写归因分解表、不写跨 step 对比**（用户要求时才补）：
1. 节标题一行：`<节名> · <UI起>~<UI止>ms · <dur>ms · 占 P1 x% · 设备状态: <四分类>`（时间只在标题出现一次）
2. "这节在做什么"：1~2 句语义说明（该节目的、结束意味着什么）
3. 实例级调用流程树：缩进表层级，每行 `函数 (dur)` + 行尾一句功能注释；µs 级噪音聚合一行（"xxx×N 合计 y ms"）
4. **禁止**：abs 时间戳、逐行 @时间、"无 python 帧/native 空隙"类说明、未经验证的推测

**简单版（三阶段总览）**：P1 推理前准备 / forward / P3 采样+同步收尾，每阶段一行（耗时+占比+瓶颈指认），末行给 P1 内瓶颈拆分

## P1 推理前准备标准节结构（Qwen3.5-VL 实测 step1+step3 校准；其他模型 VLM 节会变化）

```
P1-A 接收调度结果: shm_broadcast.dequeue{acquire_read:35取读权 → <pickle.loads>大头{
        _load_from_bytes×N + _rebuild_tensor_v2×N 反序列化张量(内含 UnpicklerWrapper+aten::empty/set_)} 
        → acquire_read:85收尾}; patched_import×N 穿插
P1-B 同步+状态更新: synchronize_input_prep(3703){npu/streams.py(183): synchronize, torch/_ops
        __call__→pytree} → _update_states{remove_request, _init_mrope_positions(VLM),
        add_request, _may_reorder_batch, refresh_metadata}
P1-C 输入准备: _prepare_inputs{commit_block_table×5, repeat, _build_attn_state,
        _get_cumsum_and_arange, copy_to_gpu×N, copy_snapshot_to_gpu×2, _prepare_input_ids(最大),
        _calc_mrope_positions, compute_slot_mapping, _calc_spec_decode_metadata,
        _sync_num_accepted_tokens, _correct_optimistic_seq_lens_cpu}; 首批H2D在此→设备脱离Preparing;
        节外尾部: _determine_batch_execution_and_padding → correct_spec_decode_token_counts →
        mamba 前置(preprocess_mamba → stage_postprocess_inputs_to_gpu)
P1-D 注意力元数据: _build_attention_metadata{attention_v1.py(276): build → _build_attn_group×4 →
        gdn_attn_builder.build×3{mamba_get_block_table_tensor, _stable_argsort_for_npu,
        _build_non_spec_chunked_prefill_metadata(主体), _build_prefill_has_initial_state_...}}
        → 尾部 synchronize_input_prep(3714)
P1-E VLM视觉编码: _preprocess{_execute_mm_encoder{group_and_batch_mm_kwargs → embed_multimodal →
        _process_image_input → Qwen3_VisionTransformer{PatchEmbed → prepare_encoder_metadata →
        VisionBlock×27 → PatchMerger}}}; 纯LLM模型无此节; 唯一有设备活动的节
→ model forward (qwen3_5.py:626)
```

- P1 期间设备基本全 free（仅 P1-E 有 computing/comm）；P1-A~E 结束才进入 forward
- forward 期间调度通道仍有 ~0.1ms 小 dequeue（纯控制消息、无张量），每 step 数次

## P2 模型前向标准结构（Qwen3-Next 混合架构实测；其他模型层结构不同，方法通用）

**P2 锚点与模式**：窗口 = `_model_forward` 实例（见消歧节锚点铁律）；执行模式三分（整图回放 / FX eager / eager）先判别再套结构。

**FX eager 步（prefill，本例 step0/1/3）**——torch.compile + acl_graph 路径下 python 树在图边界拍平（`<string>(19): forward` 内只剩 `torch/_ops.py(818): __call__`），**但图内自定义算子保留完整 python 路径**。P2 流程分析 = python 树（具名函数）+ cpu_op 层（算子语义）双层重建；层结构用**算子计数印证**：all_reduce×2/层、add_rms_norm_bias×2/层、npu_swiglu×1/层、unquantized_gemm 总数。

包装链（python 层，acl_graph.py:138=ACLGraphWrapper 内联执行、895=torch_npu FX eager）：`_model_forward` → `compilation/acl_graph.py(138): __call__（模式不匹配→内联）` → `qwen3_5.py(626): forward` → `qwen3_next.py(505): forward` → `<string>(4): execution_fn` → aot_autograd → npu_fx_compiler → `acl_graph.py(895): fx_run_eagerly` → `<string>(19): forward`（eager 执行主体）

```
GDN 层(×48, ~8.4ms)——层头 qkvz/ba proj gemm 在 core 外:
  qwen_gdn_attention_core(qwen_gdn_linear_attn.py:1699) → _forward_core(ops/gdn.py:147):
    npu_causal_conv1d_custom×2(cpu_op层,无python帧) → rearrange_mixed_qkv×2(torch.split)
    → fused_gdn_gating(device_op) → l2norm_fwd×2 + clear_ssm_states(triton)
    → chunk_gated_delta_rule(fla/chunk.py:263, 主体3.4ms){chunk_local_cumsum,
      chunk_scaled_dot_kkt_fwd, solve_tril, recompute_w_u_fwd, fla/l2norm_fwd×2}
  → 状态缓存 index_put_/triton_kernel_wrapper(0.85ms) → o_proj → all_reduce#1
  → add_rms_norm_bias → MLP{gate_up gemm → npu_swiglu → down gemm} → all_reduce#2 → norm
全注意力层(×16, ~1.7ms):
  qkv gemm → triton_split_qkv_rmsnorm_mrope(0.43ms) → unified_attention_with_output
  {attention_v1.forward{reshape_and_cache(KV缓存) → forward_fused_infer_attention
  → npu_fused_infer_attention_score}} → o_proj → all_reduce → norm → MLP → all_reduce → norm
```

- 层循环：16 组 × [3×GDN + 1×全注意力] = 64 层；GDN 层耗时 ≈ 5×全注意力层（chunk_gated_delta_rule 单层 3.4ms 是最大子项）
- 层边界归层：以 all_reduce 对为界；lm_head/sampling 不在 forward 内（属 P3）
- forward 期间设备可能仍大量 Free（本例 64%）：NOTIFY_WAIT/EVENT_WAIT 是 stream 间同步等待，host-bound 线索
- triton 内核 python 侧机械层（runtime/jit.py、autotuner.py、driver.py、dynamic_func）可折叠

**整图回放步（纯 decode + spec decode，本例 step2 实测 2026-09-29）**——P2 host 侧仅 ms 级：

```
_model_forward (6.06ms)
├ ACLGraphWrapper.__call__ (3.16ms)
│   ├ streams.py(79): synchronize (0.03ms)   FULL 模式 replay 前主流同步
│   └ torch_npu/npu/graphs.py(638): replay (2.98ms)   真图回放标志
└ _update_full_graph_params_if_needed (2.89ms)
    └ update_full_graph_params → attention_v1.update_graph_params
        + graph_task_update_begin/end ×16（update_stream 上更新 KV 地址等动态参数）
```

- 触发条件（源码）：`uniform_decode`（每请求恰好 `uniform_decode_query_len`=1+draft数 个 token 且全 prefill 完；spec decode 下还要求 is_all_decode）→ FULL 模式 → batch_descriptor 命中捕获图
- 回放步形态：设备在 P2~P3 异步执行整图（P3 窗口 computing 90%+），步尾同步 = 真排水（本例 59ms）；步长由设备执行决定，是典型的 device bound 步
- 算子计数仍可印证层结构（cpu_op/设备内核照常记录），只是 python 层拍平到 replay 一帧

## P3 采样与收尾标准节结构（Qwen3.5-VL + MTP spec decode 实测 step1+step3 校准；无 spec decode 模型无 draft 段）

窗口 = forward 实例结束 → ProfilerStep#N 结束（sample_tokens/end_step 常跨 step 边界，函数锚定勿裁剪）。worker_busy_loop 循环铁律：`execute_model → handle_output → sample_tokens(→end_step) → handle_output`——**handle_output ×2/步**（queue.put 回传结果，0.04ms）；detokenize 在调度进程执行，worker trace 不可见。

```
P3-A logits: compute_logits{qwen3_vl.py(2844) → qwen3_5.py(515) → AscendLogitsProcessor
        {lm_head gemm → _gather_logits(TP all_gather)}} → execute_model 收尾(~0.2ms) →
        handle_output#1
P3-B 采样+draft（sample_tokens = _sample_tokens_orig + end_step，P3 主体 90%+）:
  采样: _sample{AscendRejectionSampler{AscendSampler{apply_logits_processors → sample
        {apply_temperature → TopKTopP{random_sample{npu_stream_switch 换 aux 流}}}}
        → apply_sampling_constraints{expand_batch_to_tokens×3 (expand_triton)}
        → rejection_sample{generate_uniform_probs, sample_recovered_tokens (triton×2)}}}
        → _bookkeeping_sync（0.3~0.4ms 同步点）
  draft propose: propose_draft_token_ids{prepare_next_token_ids_padded{copy_to_gpu} →
        _copy_valid_sampled_token_count（aux 流 D2H 采样计数）→ prepare_inputs_padded
        (triton) → _propose{[draft 轮×N(本例3)]: embed_input_ids{embedding +
        pad_reduce(all_reduce)} → attn build/build_for_drafting → shallow_copy_metadata →
         MTP forward（FX eager，单层 transformer: gemm → all_gather →
        triton_split_qkv_rmsnorm_mrope → unified_attention_with_output → gemm×3 →
        all_reduce×2）→ compute_logits(lm_head+all_gather) → _set_positions}}
        → finalize_kv_connector
  aux 流收尾: stream 切换(wait_event) → _update_states_after_model_execute
        {postprocess_mamba_align_gpu (triton 融合后处理)} → stream record/wait_stream
P3-C end_step: _should_profile → npu/utils.py(62): synchronize（设备级同步）→ perf_counter
```

- **end_step 的 synchronize 时长 = host/device bound 指示器**：设备积压大则同步长（step1: 12.3ms，同步期间设备 Computing 91%——真排水）；host 供数不足则瞬时返回（step3: 0.09ms，设备早已空闲）
- draft 循环的 host 间隙（embed/attention build/shallow_copy/D2H）在小 batch step 造成 ~47% free；大 prefill step 几乎 0 free（step1 _propose 窗口 free 0.5%）
- 每轮 MTP forward ~3.2~3.7ms 是 draft 段最大子项；MTP compute_logits ~0.35~0.39ms（lm_head gemm + all_gather）
- **详细版报告节划分（2026-09-28 已验收）**：P3-A logits与输出回传 / P3-B 内分 B1 拒绝采样（设备满负荷 ~95% computing）、B2 draft 草拟（host 间隙致 ~43% free）、B3 aux 流收尾（~61% free）/ P3-C 步收尾；报告沿 M2 详细版格式（节标题四分类 + 实例级流程树 + µs 聚合）

## 已知坑

1. trace_view.json 是**单行大文件**：禁止 grep 上下文正则（会挂死），用索引
2. 事件键序两种：ph-first（host）与 name-first（设备/CANN），build_index 已双扫
3. 时间戳 JSON 带 3 位小数(us)，索引按 **ns 整数**存储，勿用浮点换算
4. 事件会**跨 step 边界**（如步尾 device synchronize），永远函数锚定，不要按 step 裁剪后搜索
5. UI 零点因工具而异，必须 calibrate；换 trace 重建索引后要重新校准
6. Free 里最大区间常是 Preparing（step 开头等 host）——报占比时若窗口含 step 起点要单独点出
7. 编译图（torch.compile+acl_graph）FX eager 段 python 树被拍平，流程分析须 python 树+cpu_op 双层重建（见 P2 标准结构）
8. **Free 禁止直接加 OA free 区间**（2026-09-29 修复）：OA 不覆盖窗口边缘（步首 ~1-2.3ms），小窗口直接加会丢九成 free（实测 2.5ms 窗口只显示 6.1%）；必须用补集 `Free = 窗口 − (Computing ∪ Communication)`。与 CANN csv 对比时 Stage 窗口口径本身不等长，三分类（Computing/Comm/CommNO）不受影响
9. **整图回放步没有模型级 forward python 帧**（图回放不进 python）：P2 锚点必须用 `_model_forward`，否则误回退 `_execute_model_orig`（窗口混入 P1）；图捕获发生在 profiler 启动前（引擎初始化 dummy run），trace 内无 capture 帧——看到 `graphs.py(638): replay` 才是真图回放，`fx_run_eagerly` 是 FX 图逐算子 eager（名字无 replay，别叫回放）
10. **生产 vllm-ascend 常为 tag + 内部补丁，行号会漂移**：识别特征 = `*_orig` 包装函数（`_execute_model_orig`/`_sample_tokens_orig`，tag 里没有）；先查 `vllm-src/knowledge/<版本>.md` 的行号映射表，行号锚定失败时按函数名+邻近文件行号偏移推断
11. **四分类在报告中必须可加和**：Computing + 未重叠 + Free = 100%；用户看到加不满 100% 会质疑全部分析（实测教训）
12. **各层 pid 不固定**（2026-10-14 修复）：8B 环境实测 python=149/设备=153024/OA=153568，与 27B 的 176/180672/181216 完全不同——写任何查询一律从 meta 读 pid（`pids()`），OA 按 name 匹配（任意 pid）；硬编码 176 会导致锚点全部找不到、AI 事件全部漏分（实测教训）
13. **报告列语义必须自解释**（2026-10-16 需求方反馈）：批成分表旧"decode（带 draft）"列只数带 draft 的 decode，无 spec decode 模型（8B）恒 0，与"新增 prefill=0"并列 = 看起来批内没有请求；decode 列 = 批内 − 新增，draft 另列"其中带draft"（修法见消歧节）。同日修复模型结构图溢出：`.mslayer`（×N 层单元）原为不可断行 inline-flex 一整行，层多必超 sect 容器——加 `flex-wrap:wrap;max-width:100%`

## 占比问题回答模板

```
函数: <全名> ([cat] pid=tid)
时段: UI a~b ms (dur=x.xxxms)  [占 step N 的 y%]
  Computing     xx.xx% (x.xxxms)
  Communication xx.xx% (x.xxxms)
  Free          xx.xx% (x.xxxms)  (含 Preparing 则注明)
解读: 一句话（设备忙/闲、host 是否阻塞、是否符合该函数语义）
```

## 无栈数据工作流（降级模式）

trace 无 python_function 事件时，以下路径仍可用（**已在真实 without-stack 数据全流程复验，2026-09-28**）：

- **可用**：建索引/校准、`window`、`span`（锚定设备内核或 cpu_op）、四分类、`chain`（容器链退化为 ProfilerStep#/cpu_op 外层 + 跨层 CANN 实现，实测 ProfilerStep#→vllm::maybe_pad_and_reduce→vllm::all_reduce 链完整）、`report`（无 forward 锚点→P1-only 如实输出；end_sync 自动回退 CANN 层 `aclrtSynchronizeDevice*`，与 api 聚合交叉验证一致；waits/comm/api 不受影响）、`comm`、`api`、`report_html`（2026-09-30 修复 w2=None 崩溃与空火焰图；火焰图节显示不可用提示）
- **不可用**：python 调用树、函数级消歧启发式、P1~P3 节结构锚定（这些锚点全是 python 帧）
- **替代方法**：`calltree.py <db> <起> <止> --cat cpu_op` 出算子层树（折叠规则对 cpu_op 名天然不生效）；流程语义从 cpu_op 算子序列 + 设备内核 + CANN host API 三层重建

## HTML 报告约定（report_html.py，2026-09-28 校准；2026-09-29 修订×2）

**结构**：单文件 HTML——**首页**（**hero 横幅 = 整窗总述**：大字号标题行（步数+总时长+一句判定）+ Free/Computing/Comm 大数字 + 整窗大条（mbar big）+ 图例，必须醒目、不能写成普通 kv 注释行；阶段总表：每步一行 = 三阶段耗时+四分类+P2 执行模式、end_sync、判定色标，末行合计；**批成分表**：每步批内请求数/decode（=批内−新增，带不带 draft 都算）/其中带draft/新增prefill/视觉编码/批类型（混合批/纯decode/纯prefill/含prefill续算，取证方法见消歧节"批成分判定"）；NPU Free 时间去向表）+ **每 step 一页**（① 阶段总览框：P1/P2/P3 耗时+四分类着色表+小条图例 ② 阶段火焰图 ③ P1/P3 锚点切节树 ④ P2 骨架+执行模式文案+图内算子计数表+代表实例）。莫兰迪配色，四分类四色（Computing 深钢蓝 #4A78A0 / Comm 陶土 / 未重叠 灰玫瑰 / Free 浅鼠尾草绿 #A6BE9E）——Computing 与 Free 必须高对比（深vs浅+蓝vs绿，2026-09-29 需求方反馈低对比看不清）。

**四分类口径**：Free = 窗口 − (Computing ∪ Communication)（补集，OA 缺口计入）；任何节/表格 Computing+未重叠+Free=100%，页面 note 必须写明。

**树行尾设备状态小条（mbar，2026-09-29 增补）**：每个函数行右侧**右对齐定宽 132px** 画设备状态条，表示**该函数时段内**的四分类：
- comm == commNO（无重叠）→ 三段 `[Computing 深钢蓝 | Communication 陶土 | Free 浅绿]`（加和=100%）
- comm > commNO → 四段 `[纯Computing | 重叠通信(浅陶土 #CFAF87) | 未重叠通信(陶土) | Free]`（加和=100%，浅陶土=被计算覆盖的通信）
- 悬停 title 给精确 ms；<100µs 的行不画（噪音）；聚合行（×N）用最长实例窗口；每棵树用 `WindowClasses` 一次加载区间、逐节点内存计算（勿逐节点查库）
- 布局：`.rowline` flex 行 + `margin-left:auto` + `flex:0 0 132px` → 所有行 bar 固定终点=容器右缘、起点=终点−132px（块级布局嵌套列表右缘天然对齐，**勿加负 margin 补偿**——会层层右漂，实测踩坑）
- 图例（BAR_LEGEND）放在首页 hero 和每步阶段总览框

**帧/树选择原则（白名单驱动，勿改回全量栈）**：
- `KEY_PATTERNS` 白名单 = 校准关键函数全清单（P1-A~E 含 `_update_states` 子层 `remove_request/_zero_block_ids/register_request/...`、P1-C 全部子项、P1-D builder 内部含 `_treat_single_token_.../_attach_*/_build_actual_seq_lengths`、P2 骨架、P3 采样+draft 全链、整图回放链 `_model_forward/ACLGraphWrapper.__call__/replay/update_full_graph_params/update_graph_params/graph_task_update_*`）；祖先连通保留
- `MECH_LIFT` 机械折叠（就地折叠不进图）：编译包装链（decorators/aot_compile/caching/piecewise/compile_fx/aot_autograd/runtime_wrappers/inner_fn/g）、autograd apply、dynamo `_fn`、triton jit/driver/backends、`<built-in function>`、`__getattr__`/`__torch_function__`/`_patched_environ_get` 等属性访问与环境读取噪音
- **同名父子合并**：装饰器/继承双层同名（`_update_states→_update_states`、`sample_tokens→sample_tokens`、`propose_draft_token_ids→propose_draft_token_ids`）只留一帧
- **同名兄弟聚合**（2026-09-29 增补）：同一父下同名实例合并 `×N · 合计 · 单次min~max`，子树取最长实例；**仅树视图聚合，火焰图不聚合**
- **显示**：裸函数名（无路径、无行号；仅同名泛型函数如 `forward/load/put` 保留最短文件名前缀），每行 `函数 + 时长 + 功能注释`（`COMMENTS` 表，与验收报告行尾注释一致）；µs 叶子聚合
- **P2 节**：标题带锚点+执行模式（`_model_forward` 锚点优先；模式由窗口内 `): replay`/`fx_run_eagerly` 判别），正文用模式对应文案（P2_MODE_TEXT）；整图回放步的 verdict 不加"forward供数不足"后缀（P2 host 窗口极短，free 语义不同）
- **P2 火焰图特例**：骨架只留 `qwen3_5.forward → qwen3_next.forward → npu_fx_compiler → acl_graph(→fx_run_eagerly) → <string>: forward`；编译中间层与其下 GDN/attention 层循环内部**不进火焰图**（层细节在算子计数表+代表实例；代表实例 = python+cpu_op **双层重建**，可见 `aten::slice/view`、`_C_ascend::npu_causal_conv1d_custom`、`aclnn*` 等）
- **模型结构图（P2 框尾，2026-10-14 增补）**：从主执行流（内核数最多的 stream）设备内核 ts 时序推导——attention 类内核作层锚点分段（无 attention 时取段长最均匀的锚点），重复单元（众数段+锚点）旋转至 QKV 开头，头部=首层前缀、尾部=末层后缀（线性展开，允许与循环重复）；角色标注 QKV/O/MLP-up/MLP-down/Attention/Norm/Act/KV写 按位置推断。**整图回放步取整个 step 窗口**（设备异步执行，P2 host 窗口只截到部分内核），eager/FX eager 取 forward 窗口。无侧车（kernel_details.csv）时这是唯一能看到模型结构的入口
- **交互**：点帧放大子树/点上层帧回退/重置按钮；顶部**固定概览条**不受缩放影响、点击跳阶段、带视口指示器
- P1 节切分锚点：`synchronize_input_prep/_update_states/_prepare_inputs/_build_attention_metadata/_preprocess`；P3：`sample_tokens/_sample/propose_draft_token_ids/end_step`；P2 窗口 = `_model_forward`（缺失时才退化 `_execute_model_orig` 并标注）

## 局限

- P2 锚点 `_model_forward` 为 vllm-ascend v0.23.0rc1(+补丁) 实测；其他版本先查 vllm-src/knowledge 行号映射
- P1~P3 标准节结构来自 Qwen3.5-VL + MTP + torch.compile 单一场景，其他模型/配置需复验（纯 LLM 无 P1-E、无 spec decode 无 draft 段、纯 decode 步 P1 极短且 P2 为整图回放）
