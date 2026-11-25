---
name: llm-cache-canonicalization
description: >
  规范缓存文件的版本管理与一致性：md5 定版、跨机分发、多文件合并、缓存漂移判定与每轮恢复。
  保证"同一份缓存、同一份代码、同一组数字"。可单独用于任何依赖回放缓存的基准。
  场景入口类触发：缓存漂移、Misses 大于 0、多机结果不一致、canon 文件、缓存合并、缓存 md5。
  工具类触发：缓存版本管理、缓存分发、跨机一致性核对。
---

# 缓存规范化与版本管理

## 何时用

- 两台机器要复跑同一套基准；
- `ingest` 出现 `Misses: n>0`；
- 缓存条目在运行中被写回，导致下一轮口径变化。

## 规则

1. **单一 canon 文件**：以 `cache/canon.json` 为唯一真源，md5 记入报告与交付清单；
   实测基线为 `316739ce9c0474c638b8930b9fe0fa04`（103 MB / 4680 条），两台机器必须一致。
2. **每轮前恢复**：运行会把新条目写回缓存，所以每轮开始前从 canon 副本还原，否则命中率逐轮漂移。
3. **漂移即作废**：`Misses > 0` 说明有 prompt 不在规范缓存里，先合并再重跑，不允许"带 miss 跑完"。
4. **合并顺序显式**：`--merge a.json b.json` 后写覆盖先写；合并后重新 `--verify` 与 `--md5`。
5. **变更留痕**：缓存更新（换模型、补样本）必须新开版本号并在报告里写清，历史数字只能与新版本对比。

## 校验

脚本不在本 skill 目录内，位于同族 `llm-call-replay-cache/scripts/llm_replay_cache.py`
（安装后为 `skills/llm-call-replay-cache/scripts/llm_replay_cache.py`）。单独使用本 skill 时，
先装/复制该脚本，或按下列相对路径调用。

```bash
llm_replay_cache.py --verify cache/canon.json   # entries / unique / size
llm_replay_cache.py --md5    cache/canon.json   # 与交付清单比对
```

## 与其他 skill 的关系

- 缓存怎么产生：`llm-call-replay-cache`
- 缓存是否被合规使用：`agent-memory-bench-gate`（0 miss 门禁）
