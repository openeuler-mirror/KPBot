# 溯源、归档与复验全量

## 1. 溯源表（定论文档 §4）
六列：`镜像 tag | image id | 基线版本 | 补丁/改动 | 结果文档 | 状态`，tag + image id 双锚定（image id 即
"双锚定"的第二列，不可省），覆盖全部镜像（含废弃/中间版）。

| 镜像 tag | image id | 基线 | 补丁/改动 | 结果文档 | 状态 |
|---|---|---|---|---|---|
| `<milvus-image:top2-v2>` | <image-id> | runtime-v1 | 0002-top2-go-alloc.patch | 测量记录 | 保留 |
| `<milvus-image:top2-v1>` | <image-id> | 裸二进制 | 裸镜像误打 tag | 修复记录 | 作废 |
| `<mem0-server-image:flatidx-v1>` | <image-id> | 2.0.14 | 0001/0002 flatidx | 对比记录 | 保留 |
| `<runner-image:flatidx-seed-v1>` | <image-id> | runner | seed _insert_batch | seed 修复记录 | 保留 |
| `<mock-image>` | <image-id> | mock | — | — | 保留 |
| `<milvus-image:sve-runtime-v1>` | <image-id> | — | 基线（SVE+simdjson） | 基线数据记录 | 历史基线 |
| top1b | — | top2-v2 | faiss 展开/预取 | 展开/预取测量记录 | 作废 |

## 2. kept 清单（按实际参与组件登记）
**按本次测量实际参与的组件逐一登记**，四角色只是本案例示例；只有单个服务、无 milvus/mock 的项目就只登记
那个组件，不强行凑齐。
| 角色 | tag | image id | 优化手段 |
|---|---|---|---|
| milvus | `...-top2-v2` | <image-id> | TOP2 Go 分配削减（4文件8处）+ SVE + simdjson |
| mem0 | `...-flatidx-v1` | <image-id> | flatidx 展平索引 |
| runner | `...-flatidx-seed-v1` | <image-id> | seed schema 感知 6 列插入 |
| mock | `...` | <image-id> | 回放 |

## 3. 归档分层
```
docs/        定论层 + 过程记录，唯一活事实源
opt-trace/   <编号>/{product（做了什么）, data（测出什么）, reports（结论）}
BOARD.md     进度事实源
```
- `opt-trace` 编号：00-baseline / 01-flatidx / 02-top2 / 03-top1b-faiss / 04-top1-scope / 05-top3-entity-boost / 06-gomemlimit。
- 副本标注"复制自原文"，修改只改唯一活事实源。

## 4. 负结果记账（四列）
| 做了什么 | 结果 | 放弃原因 | 正确方向 |
|---|---|---|---|
| TOP3 实体 boost 削减 | grep 0 命中、keyword_search 返回 None | 证伪（零触发） | 无 |
| TOP1(c) 距离并行化 | 带宽受限 ~8%、核已占满 | 证伪（带宽受限，待实测带宽确认） | 降访存流量（动召回，超范围） |
| TOP1(b) 双累加器+预取 | 1 轮无改善、.so confound | 无增益 + 无法归因 | 需干净构建路径重测 |
| GOMEMLIMIT 注入 | 堆峰 637MB、软限未触发、-5.2% | 本轮该配置未观察到收益 | 无 |

> **GOMEMLIMIT 结论要限定**："软限从未触发"只说明该配置下本轮未观察到收益，**不能**推出"GC 非瓶颈"——
> 即便软限没触发，GC 仍可能在原有阈值下占用大量 CPU。要排除 GC 瓶颈还需同窗口的 GC CPU 与停顿数据，
> 缺则结论停留在"本轮该配置未观察到收益"。

放弃后环境还原：源码、入口、配置回到保留态，避免污染共享环境。

## 5. 等价重构复验（submodule 重组）
- 两处回归：①版本转发变量丢失；②采集器 SIGINT 失效。
- 复验锚：实跑计时 + **请求计数 23420 逐位一致**。
- **但请求计数一致不能单独判"回归已修复"**：容器回落到旧镜像、或采集器 SIGINT 后未落盘，回放仍可完成 23420 次。
  两项回归分别核对：①**运行容器实际镜像 ID / 摘要**与预期构建一致（不只信 tag）；②**采集器退出状态** +
  **相位产物可解析性**（`perf-data/<phase>/meta.json` 可解析、`phase-markers.log` 齐全）。三项齐备才判修复。
- 复验项：版本转发、采集器 SIGINT、结果目录结构、镜像 tag 解析。

## 6. 单一事实源 + 契约
- `modules/benchmark/manifest.yaml`：四角色 tag + index/seed 契约（原 RELEASE.txt 收敛于此）。
- `contracts/`：`image-manifest.schema.json`、`phase-marker.schema.json`、`README.md`。
- 版本转发做显式校验（校验和清单），禁止静默回落到旧版本。

## 7. 子 agent 纪律
- 边界无交集；共享事实经 `contracts/` 与 `docs/` 传递，不靠会话上下文；每 agent 绑定唯一产出路径；开工前读溯源表"勿重造"。
- Conventional Commits；大件（gcc10-toolchain/images/*.tar/tantivy vendor/构建树）只落盘不入 git；每里程碑一 commit。

## 8. 项目重组与上游合入（工程纪律补充）
- **重组**：从扁平工程树拆为「主仓 + 四 submodule」（modules/mem0、milvus、benchmark、perf-tools），补丁式落库（子仓只提交 `.patch` + 构建脚本 + manifest），大件（gcc10-toolchain / images/*.tar / tantivy vendor / 构建树 21G）只落盘不入 git。
- **重组复验**：两处回归（版本转发变量丢失、采集器 SIGINT 失效）→ 实跑计时 + 请求计数 23420 逐位一致才算过关。
- **上游合入（benchmark 脚本快照）**：上游 oct-26 zip 只含 `scripts/`，本地 fork 在 multi-runner/manifest 参数化上领先 → 采用**三方对比法**（base / upstream / local），识别"上游真正新增" vs "本地已更完整"，语义移植而非机械覆盖；`seed_milvus_baseline.py` 上游无新内容则跳过。原则：机械覆盖会回退本地能力，合入前先三方对齐。
