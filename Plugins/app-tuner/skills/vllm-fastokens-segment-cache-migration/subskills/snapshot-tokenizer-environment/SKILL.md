---
name: snapshot-tokenizer-environment
description: >
  记录 vLLM 迁移前的依赖、相关环境变量和 fastokens 原构建，生成回退说明。
---

# 迁移快照与回退准备

## 操作流程

使用服务的 Python，在切换前对原环境生成快照：

```bash
"$PY" "$S/scripts/fastokens_segment_cache.py" snapshot --out "$SNAPSHOT"
```

Linux 上要保存运行配置时，按实际服务追加 `--pid`，可多次指定。未指定进程时 `env.json` 只记录当前 shell；进程项为 `null` 表示未读到，不能当作变量未设置。生成快照时不要加载候选目录。

1. 检查 `packages.txt`、`env.json`、`fastokens.json` 和 `RESTORE.md`。如果原先安装了 fastokens，核对重建的 wheel、wheel SHA-256 和原生库哈希。相同版本号可能对应不同构建，不能用版本号替代原构建。
2. 原环境未安装 fastokens 时，将「原先未安装」记为回退基线。如果原来已安装但 wheel 重建失败，记录 `fastokens.json` 中的原因及原安装来源，注明仍需准备的回退制品。

## 输出

交付快照目录、`RESTORE.md` 和回退准备状态。由用户执行回退；回退后可用 [当前分词后端分析](../inspect-tokenizer-backend/SKILL.md) 对照变量与原生库哈希。
