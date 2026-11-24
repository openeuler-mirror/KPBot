---
name: verify-tokenizer-replay
description: >
  对比 vLLM 当前分词配置与候选 fastokens 段缓存开启组，检查 token ID、并发、缓存命中、耗时和 RSS。
---

# 分词回放验证

## 操作流程

使用服务的 Python、分词器、模式及 revision。服务指定聊天模板时，加 `--chat-template` 使用同一文件。从已验证的候选目录取 fastokens；环境内已有目标版本且已核对时，可省略 `--overlay`。两组读取同一份回放 JSONL。

```bash
"$PY" "$S/scripts/fastokens_segment_cache.py" verify --model "$MODEL" \
    --sessions "$SESSIONS" --overlay "$OVERLAY" \
    --orig-env VLLM_USE_FASTOKENS= --orig-env FASTOKENS_SEGMENT_CACHE= \
    --cache-mib "$CACHE_MIB" --out "$OUT"
```

上例为当前基线使用 HF tokenizers 的情形。基线已使用 fastokens 时，将 `--orig-env` 改为服务实际的开关和缓存配置；空值表示删除对应变量，避免继承当前 shell 的设置。运行基线时不要将候选目录放进父进程的 `PYTHONPATH`。

两组在独立子进程运行，默认三轮交替顺序，首轮按会话分配线程。需要检查并发时使用两个或更多线程，并提供至少两个会话。计时在无其他高负载任务时进行。

## 回放验收

按 `report.json` 的 `correctness_pass`、`problems` 和各组 `concurrency` 核对以下条件：

- 两组各轮均完成，未出现异常或超时。
- 两组聊天模板渲染结果相同，逐请求 token ID 一致，各轮分词结果一致。
- 两组均实际执行多线程检查，结果无不一致或异常。没有并发证据时记为「未测」，不能据此通过迁移验收。
- 段缓存开启组实际使用 fastokens，且回放期间的缓存命中数大于 0。没有命中统计或命中数为 0 时，回放验证不通过。

## 输出

交付 `report.json`、`report.md` 和本次运行参数。耗时与 RSS 是分词器层测量，成功退出不代表达到性能阈值。两组结果反映整体配置差异，不把收益单独归因于段缓存。
