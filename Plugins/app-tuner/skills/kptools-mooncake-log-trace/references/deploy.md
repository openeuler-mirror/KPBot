# .so 部署替换与回滚

> 宿主机上操作，用 `scripts/deploy_mooncake_so.sh`；本文件说明边界与手工兜底。

## 1. 目标 .so 的确定

以**运行中引擎进程实际 mmap 的库**为准（`/proc/<pid>/maps`），不是"看起来像"的那份：

- 产物清单来自 `kptools-mooncake-instrumentation`；目标侧可用 `grep mooncake /proc/<pid>/maps`（运行中引擎）或 `find` 核对现役库路径 + md5；
- Python 包目录里可能有多份 .so（.pyi、缓存、多版本共存），替换错文件 = 白干一次；
- mooncake 客户端可能拆成多个 .so（client + transfer engine 依赖），打点涉及的全换。

## 2. 部署纪律（脚本已内置，手工兜底时同样遵守）

1. **先校验后写入**：新 .so 必须先过 `strings | grep MKT` 与 md5 清单核对，再往容器里拷；
2. **备份**：宿主机 `mktemp -d /tmp/mkt_bak.<容器名>.XXXXXX`，`docker cp` 拉出全部目标 .so，写 MANIFEST（容器内路径 ↔ 备份文件 ↔ 原 md5），回读校验备份完整；
3. **替换**：`docker cp` 新 .so 到原路径（stopped 容器同样有效，`docker exec` 写文件对 stopped 容器无效）；
4. **回读校验**：容器内 `md5sum` 与编译产物 md5 逐一比对，不一致即回滚；
5. **不自动重启**：.so 替换对运行中进程无效（旧库已 mmap），提示用户重启引擎。

## 3. 重启与 smoke

1. 重启顺序参考 kptools-tpsplit-log-trace 第 3 节第 7 步（P 起 mooncake_master → P 引擎 → D 引擎 → proxy）；
2. 重启后先核进程加载：`grep mooncake /proc/<新pid>/maps`，md5 应等于新 .so；
3. 发 1 条真实请求，`grep -E "\[MKT_TTF\]|\[MKT_FINE\]" <引擎日志>` 应有成对输出；
4. 核对打点规格一致性（keys 相同、FINE 五段和 ≈ getmem_ms；契约见 `kptools-mooncake-instrumentation` 的 references/instrumentation.md 第 4 节），不符说明打点挂错阶段，退回该 skill。

## 4. 回滚

```bash
# 按 MANIFEST 逐条 docker cp 回原路径，md5 回读校验，再重启引擎
bash scripts/deploy_mooncake_so.sh --rollback <备份目录> <容器名>
```

回滚后同样要 smoke（MKT 应消失、引擎正常服务）。

## 5. 已知坑

| 现象 | 原因 | 处置 |
|---|---|---|
| 替换后重启，进程仍加载旧库 | 路径不对 / 有软链 / 多副本 | 以 `/proc/<pid>/maps` 为准重新定位 |
| 引擎起不来（import/符号错误） | .so 版本错配（源码与现役版本不一致） | 立即回滚；版本问题退回 `kptools-mooncake-instrumentation` 重新锚定 |
| 引擎正常但无 MKT 输出 | glog 级别 / Ray 去重 / 无流量 | 见 SKILL.md 第 6 节 |
| smoke 出 MKT 但数值全 0 | 打点变量没接上计时区间 / 全 miss 批次 | 发真实请求复测；仍 0 则核对挂点 |
