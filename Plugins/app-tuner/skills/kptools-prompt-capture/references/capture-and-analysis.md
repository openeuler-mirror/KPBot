# 配置、采集与分析

## 配置准备

脚本使用 Python 3.9 及以上版本。下文 `$S` 为安装后的技能目录，`$PY` 为 proxy 所用的 Python 解释器。容器化部署时，控制命令在 proxy 所在容器执行，路径使用容器内路径。配置初始化在 worker 启动前完成；数据和报告写入受限的独立目录，不写入模型目录或版本库。

```bash
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json init
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json load "$S/assets/macro.json"
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json set out_dir=/data/pc/data max_total_gb=5
```

控制器返回 0 表示已确认生效，2 表示参数或文件错误，4 表示未确认生效或没有 worker。worker 尚未启动时，配置可写入但返回 4，启动后需再次核验。控制器仅操作配置与状态，不连接远端、替换源码或重启服务。

| 目标 | 配置 |
| --- | --- |
| 宏观元数据 | `macro.json`：全量元数据、无正文、无 golden、输出统计 |
| 宏观含正文特征 | 授权后设 `text_rate=0.05` 或用户指定比例；统计只代表全文样本 |
| 指定会话 | 加载 `session.json` 后设 `follow_new=0 follow_sessions='["实际ID"]'` |
| 自动跟踪会话 | `session.json` 默认 `follow_new=1`，按用户确认值调整；可设 `follow_clients='["opencode"]'` |
| 输出全文 | 另行确认后设置 `output=text` 和/或 `follow_output=text` |
| golden | 默认关闭，兼容性确认后按授权设 `gold_rate` 或 `follow_gold=true` |

元数据级请求的 `params` 仅保留下表中名称与值类型均符合的参数。其余参数在 `param_meta` 中记录值类型、序列化字节数与内容哈希，不保存参数原值。

| 参数 | 保留条件 |
|---|---|
| `model` | 字符串，作为模型标识记录 |
| `max_tokens`、`max_completion_tokens`、`n`、`seed`、`top_k`、`top_logprobs` | 整数，不含布尔值 |
| `temperature`、`top_p`、`min_p`、`presence_penalty`、`frequency_penalty`、`repetition_penalty` | 整数或有限浮点数，不含布尔值 |
| `stream`、`echo`、`logprobs`、`ignore_eos` | 布尔值 |
| `tool_choice` | `none`、`auto` 或 `required` |
| `reasoning_effort` | `none`、`minimal`、`low`、`medium` 或 `high` |

`chat_template`、`chat_template_kwargs`、JSON Schema、停止字符串及自定义参数均按摘要记录，不因长度较短而保留原文。全文级请求仍保留原参数，以供还原与分词分析。新规则不清除历史采集文件中已保存的原值。

宏观分析与会话分析可同时进行。普通流量按 `text_rate` 抽样记录全文，`follow_*` 为选定会话增加连续记录；其他流量仍按抽样配置记录。跟踪会话不受普通全文限速影响，但仍受请求体、队列和磁盘限制。

`follow_sessions` 支持 ID 或前缀匹配，设置前需确认前缀覆盖范围。`follow_new=N` 为每个 worker 的自动选择配额，其他 worker 约一秒后同步跟踪集合，因此实际会话总数可能超过 N，开头的请求也可能未被记录。状态不跨节点共享。多 worker 路由、配置生效时间、记录丢弃和会话压缩均可能影响完整性。

修改跟踪范围或采集窗口、关闭采集后，清空原有跟踪集合与自动选择记录，后续请求按新配置选择。仅调整 golden、输出方式或普通流量抽样率时保留跟踪集合。worker 之间仅同步同一配置文件版本的名单，旧版本状态与已排队的旧同步结果不再加入。配置变更前已开始记录的请求仍按原级别收尾。

请求头无会话 ID 时，以首段消息推断 `fp:` 会话，报告中应标注推断及可能的归组误差。采集开始后首次记录的会话，不一定是新建业务会话。

## 采集控制

授权部署和健康检查通过后：

```bash
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json on --duration 600 --metadata-only
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json status --json
```

开启前用 `show` 核对已确认的配置。仅采元数据时，`text_rate`、`gold_rate`、`follow_new` 均为 0，`follow_sessions` 为空，`follow_gold` 为 false，`output` 和 `follow_output` 不得为 `text`。`--metadata-only` 遇到冲突会退出且不写配置。有正文权限时按授权分别设置普通采样、会话跟踪、输出与 golden，再去掉该参数。

`on` 的 `--duration` 必须为有限正数，单位为秒。控制器将其换算为绝对截止时间，采集模块逐请求检查，并在后台更新有效 enabled 状态。终端断开不影响截止检查，proxy 重启也不延长时限。配置文件中的 enabled 可能仍为 true，实际状态以各 worker 的 enabled 为准。截止时间依赖系统时钟，时钟异常时需重新核对并在报告中说明。

状态检查包括预期 worker 数、配置版本、配置错误、写盘进程 PID，以及写入计数和分段文件是否增长。没有流量时记录为零个请求，不能据此判定已完成有效采集。检查丢弃计数、`writer_deaths`、`gold_missing` 和预算状态；持续异常时执行 `off` 并报告，不重启业务服务。

`queue_max_mb`、`max_body_mb`、`max_output_kb` 分别限制缓冲、请求体和输出大小；`rotate_mb/min` 控制分段。`max_total_gb` 定期检查，多写盘进程并行时可能超额，不是硬磁盘配额。预算不足时停止写入并增加 `drop_budget`，检查发现后执行 `off`。需要硬配额时由用户按其文件系统配置，本技能不修改配额。

```bash
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json off
$PY "$S/scripts/capturectl.py" --config /data/pc/capture.json status --json
```

关闭后，在途请求仍可写入结束记录。等待队列排空、写盘进程退出和压缩完成；超过用户允许的等待时间时，按不完整数据记录，不终止业务进程。采集目录应与本次时间范围对应；已有目录包含多次采集时，宏观分析使用 `--since/--until` 指定范围，会话分析使用本次对应的分段文件。

## 分析入口

基础分析无需模型，不读取权重。`DATA` 是分段目录（支持参考 FORMAT=1 的 jsonl/gz），`MODEL` 是本地 tokenizer 目录而非请求中的 model 别名。报告产物应写入新的 0700 目录。

分析工具自动以 0700 创建输出目录，以 0600 创建产物。已有目录必须属于当前用户，且不能有组或其他用户权限；不满足时拒绝写入，不修改已有目录权限。已有产物或符号链接不覆盖，应改用新的输出目录。语料导出文件也采用相同限制。

```bash
$PY "$S/scripts/analyze.py" DATA --out AN
$PY "$S/scripts/session_view.py" DATA --out SV --top 3
```

宏观分析输出 `requests.csv/sessions.csv/summary.csv/report.html`；会话分析输出 `sessions.html`、按会话 ID 哈希命名的 md/json、`session_requests.csv/session_messages.csv/fleet.json`。`--top` 指定报告选取的会话数，不改变采集配额。没有符合条件的会话时，检查全文比例、会话 ID 和请求数门槛，报告原因。

会话报告默认不包含正文、预览、参数和输出，宏观报告默认不展示实际预分词片段。用户允许原文输出后，使用 `--include-text`。会话 ID、模型名、工具名和错误信息仍可能包含敏感内容，对外提供报告前需另行检查。

需要分词特征时，给分析命令增加 `--tokenizer MODEL`。部署使用 `--default-chat-template-kwargs` 时，通过 `--template-kwargs JSON` 传入相同值。自定义 tokenizer 代码需经授权后使用 `--trust-remote-code`，不会自动重试加载。存在 golden 时逐项核对 token IDs；token 数相等不代表 ID 序列一致。核对失败时，标注离线结果未验证，其余分析按可用数据继续。

```bash
$PY "$S/scripts/join_vllm_log.py" --requests AN/requests.csv --p-log P.log --d-log D.log --out JOIN
$PY "$S/scripts/export_corpus.py" DATA --format requests --out requests.jsonl
$PY "$S/scripts/export_corpus.py" DATA --format gold --split eval --out eval_gold.jsonl
$PY "$S/scripts/check_tokenizer.py" --corpus eval_gold.jsonl --tokenizer MODEL
$PY "$S/scripts/dump_requests.py" --help
```

语料导出与原始请求查看会输出原文，需用户明确要求。训练集与评估集按会话切分，推断会话的限制同上；正文经脱敏修改后，需重新核对分词结果，不再沿用原始 golden 的一致性结论。

日志关联要求 TTFT_DEBUG 格式兼容且请求 ID 相同，报告中列出匹配数和总数。未匹配的请求不计算对应内部耗时。本技能不自动部署参考 vLLM 请求 ID 补丁。普通 proxy 未接入 P/D 钩子时，对应时延列为空，标注“未测”。

`--jobs N` 用于并行内容分析，进程数应按空闲 CPU 设置。分词纯计算时间、吞吐和排队估计用于性能比较时使用 `--jobs 1`。离线重放缓存命中率与线上命中率分别标注；proxy 首块响应不一定包含首 token，P 往返包含排队与通信等时间，不是纯 prefill 耗时。

会话报告中，`new_share` 和 `template_lost_share` 的分母为有同线程上一请求、且 token 可比较的请求的 token 总数，首请求不计入。`template_lost_share_total` 的分母为所有有 token 计数请求的 token 总数。HTML 与控制台的“占全部 token”使用后者；这些比例不是服务性能收益。

## 报告要求

报告结构见 `SKILL.md` 的“报告要求”。数字注明来源文件、时间范围、抽样范围和测量层级。区分 proxy 记录与计时、后端返回、日志、离线计算和重放估计。没有有效数据时，报告采集或分析失败的原因，不给出性能结论。
