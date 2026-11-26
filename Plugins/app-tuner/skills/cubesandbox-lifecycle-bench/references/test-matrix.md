# 测试矩阵与指标口径

来源：
- CubeSandbox 仓库：https://github.com/TencentCloud/CubeSandbox
- PVM 性能指导（2026-06-03）：`docs/zh/blog/posts/2026-06-03-cubesandbox-perf-benchmark-pvm.md`
- 实际执行真源：目标源码树 `examples/cube-bench` 与 `examples/snapshot-rollback-clone`。

上游参数可能变化；执行前以所选 commit 的脚本和 `--help` 为准。本 skill 默认计划复现博客，
而不是把博客在其他硬件上的数值当验收线。

## 核心原理

- **Template create**：从 `POST /sandboxes` 到沙箱 running 的端到端延迟；并发档位串行执行，
  create-delete 同时覆盖回收路径。`wall` 是整批耗时，`avg/P95/P99` 是单请求分布。
- **density**：空机记录 MemAvailable，分批 create-only 并保持存活，观察 Host 内存随累计
  沙箱数的变化。CoW 空载开销不代表沙箱写满内存后的容量。
- **Snapshot**：对不同沙箱并发制作内存+文件系统快照；同一沙箱的快照内部串行，不能用一个
  沙箱伪造 N 路独立并发。
- **dirty_page**：在 Guest `/dev/shm` 写入指定 MiB，通过 soft-dirty 只保存变化页；实际写入量
  以 `/data/log/CubeVmm/vmm.log` 为准，理论写入量不等于实际 Dirty Page。
- **Snapshot create**：多个沙箱从同一 Snapshot 恢复，测整批 wall 与每沙箱均摊耗时。
- **Rollback**：沙箱只能回滚到自己创建的 checkpoint，因此每个并发沙箱独立打快照再回滚。
- **Clone**：从一个运行中源沙箱派生 N 个沙箱，保留内存和文件系统状态。
- **Pause/Resume**：并发暂停后再恢复。上游版本可能采用 full-memory-copy，Pause 会受匿名
  内存总量影响；必须记录实际 commit，不能套用未来 soft-dirty 实现的预期值。

## 默认矩阵

| 章节/别名 | 官方工具 | 默认档位 |
|---|---|---|
| `template_create` / `create` / `3.2` | `cube-bench` | `(c1,n20)`、`(c10,n200)`、`(c20,n300)`；`create-delete`，每档 warmup=3，独立 rounds=1 |
| `density` / `3.3` | `cube-bench -m create-only` | 依次 `(c1,n1)`、`(c4,n4)`、`(c5,n5)`、`(c10,n10)`；累计 1/5/10/20；max_live=20，max_batch=10 |
| `snapshot` / `4.1` | `bench_snapshot_concurrency.py` | c1/c5/c10；每档正式 rounds=5，另有一次热身 |
| `dirty_page` / `4.2` | `bench_snapshot_dirty.py` | 0/10/50/100/200/500/800/1024 MiB；每档 rounds=3 |
| `snapshot_create` / `4.3` | `bench_create_concurrency.py` | c1/c10/c20；每档正式 rounds=3，另有 Snapshot 恢复暖缓存 |
| `rollback` / `4.4` | `bench_rollback_concurrency.py` | c1/c5/c10；每档正式 rounds=5；dirty_mb=0 |
| `clone` / `4.5` | `bench_clone_concurrency.py` | `(n1,c1,r5)`、`(n10,c5,r3)`、`(n20,c10,r3)`；dirty_mb=0 |
| `pause_resume` / `4.6` | `bench_pause_resume_concurrency.py` | c1/c10；每档正式 rounds=5，另有一次热身 |

共展开 29 个测试命令，另有一次真实 create/run_code/delete 冒烟。

## 参数语义（不要混淆 `-n`）

| 工具 | 并发 | 数量 | 轮数 |
|---|---|---|---|
| `cube-bench` | `-c` 最大在途请求 | `-n` 每个档位正式请求总数 | 本 skill 计划的 `rounds` 是独立外层重复；`-w` 是正式统计前 warm-up 操作数 |
| Snapshot/Rollback/Pause 等 Python 脚本 | `-c` 每批沙箱/操作并发 | 每批数量通常等于并发 | `-n` 是该档内部正式测量轮数；热身由官方脚本处理 |
| Clone | `-c` 最大并发 Clone | `-n` 每轮 Clone 总数 | `--rounds` 是该档正式轮数 |

Python 脚本输出的 `wall_avg/min/p95/max` 是**该档各测量轮整批 wall 的统计**；
`per_* = wall_avg / 并发数（Clone 为 num）`。不要把这里的 `-n 5` 解释为五个请求。

## 修改计划

复制 `assets/default-plan.json` 后修改。JSON 采用严格字段校验，防止把 `concurrency` 拼错后
静默使用默认值。任何修改都会改变 SHA256，必须重新向用户展示并确认。

八个单场景 Skill 接受完整计划或仅含本章节、`schema_version=1` 的计划，并由启动器固定追加对应
section；用户不能用 `--sections` 改跑其他章节。默认计划仍共展开 29 档，各场景档数依次为
3/4/3/8/3/3/3/2。批准哈希同时绑定固定 section 与最终计划，不能用全矩阵哈希替代单场景哈希。

性能对比建议为同一档位设置至少 3 个独立外层轮次，并遵守同纪元、同模板、同清理口径。
默认 create 的 rounds=1 用于复现博客矩阵，不自动宣称统计稳定；需要稳定结论时提高到 3，
并将多轮 JSON 交给 `create-verdict.json` 判读。

## density 安全

`max_live` 是累计硬上限，`max_batch` 是单批硬上限，`min_available_mb` 是每批前后的
MemAvailable 安全水位。默认只做 20 个空载沙箱、单批不超过 10 个。提高批次或上限前必须根据
Host 内存、沙箱规格和实际负载重新评估；不得一次性创建大量实例。每批结束后先 settle，再以
API 实际新增数计算存活数并采样内存，而不是直接使用请求数。触及水位或发生失败会停止扩批
并清理本章节新增沙箱。内存安全水位和 max_batch 只是保护门，不是 OOM 的绝对保证。
