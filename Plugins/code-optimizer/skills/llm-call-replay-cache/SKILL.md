---
name: llm-call-replay-cache
description: >
  LLM / embedding / rerank 调用的录制-回放缓存：把模型调用变成确定性输入，缓存键 = 目标名 + 方法名 +
  参数 sha256（与 API key、模型地址无关）。提供 Python API 与命令行维护工具，可单独用于任何需要
  "冻结模型调用"的基准。含 record/replay/auto 三模式语义与回归测试。
  场景入口类触发：模型调用缓存、LLM 回放、embedding 缓存、rerank 缓存、record/replay、基准要确定性。
  工具类触发：缓存键设计、缓存合并、缓存校验、miss 门禁。
---

# 模型调用录制-回放缓存

## 三种模式（语义固定，不要混用）

| 模式 | 行为 |
|---|---|
| `record` | **每次都调用真实客户端并覆盖写入**（键已存在也覆盖）；用于首建或重录 |
| `replay` | 只读缓存；miss 直接抛错，用于 CI 门禁，防止偷偷出网 |
| `auto` | 命中返回，miss 才调用真实客户端并回填（默认） |

`record` 必须无条件出网：否则换了模型/改了 prompt 后重录，旧响应会被原样保留并当成"本轮录制结果"，
后续 A/B 建立在过期基线上且不报错。

## 命中判定

用**键是否存在**判命中，不要用"值是否为 None"：

```python
with cache._lock:
    found = key in cache._data
    cached = cache._data.get(key)
if found:
    cache.hit += 1
    return cache.decoder(cached)
```

被包装方法返回 `None`（空输入、过滤无结果、SDK 可空返回）时，缓存里是 JSON `null`；
若用 `is not None` 判命中，replay 会误抛 `KeyError`，auto 会反复出网，`miss == 0` 门禁永久不成立。

## 用法

```python
from llm_replay_cache import ReplayCache
cache = ReplayCache("cache/canon.json", mode="replay")
llm  = cache.wrap(raw_llm, name="llm")
emb  = cache.wrap(raw_embedder, name="embed")
rer  = cache.wrap(raw_reranker, name="rerank")
print(cache.stats())      # {'hit': 312, 'miss': 0, 'entries': N}
assert cache.miss == 0
cache.save()              # 首次建库必须显式保存：record 只更新内存与脏标志
```

命令行：

```bash
llm_replay_cache.py --verify cache/canon.json     # 条目数/唯一数/重复数/大小（有重复返回非零）
llm_replay_cache.py --merge a.json b.json -o canon.json
llm_replay_cache.py --md5 cache/canon.json        # 跨机一致性核对
```

## 建库后验证（新进程回放）

1. `record` 跑完后调用 `cache.save()` 落盘（否则进程退出即丢整批响应）；
2. 另起一个进程，用 `mode="replay"` 加载同一文件，跑同一批输入；
3. 断言 `miss == 0` 且返回值与录制时一致——这一步通过才算缓存可用。

## 重复键检测

`--verify` 用 `json.load(..., object_pairs_hook=...)` 遍历原始键值对检测重复键：
普通 `json.load` 会把同名字段静默折叠成后一个值，`len(d.keys())` 恒等于条目数，因此「重复键」必须靠 hook 才能发现。检出重复时打印键前缀并返回非零。

## 键设计

- 必须带 **目标名**：LLM 与 reranker 参数可能撞车，只按方法与参数会把两类调用串味。
- 参数用 `json.dumps(sort_keys=True, default=str)` 后取 sha256；返回值用 `encoder`/`decoder`
  处理不可直接 JSON 化的类型。
- 被缓存方法必须是 async 协程。

## 约束与风险

- 缓存条目与模型版本绑定：换模型必须重录，否则回放的是旧行为。
- `record` 期间的数据不能用于收益结论。
- 缓存文件需按 md5 定版并随交付分发（见 `llm-cache-canonicalization`）。

## 自测

`scripts/test_llm_replay_cache.py` 覆盖：record→replay 等价、目标名不同即 miss、参数不同即 miss、
sync 属性透传、auto 二次命中、record 覆盖旧键、返回 None 的命中往返、key 稳定性、CLI 合并。
