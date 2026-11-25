# 阶段门控采集器（collect_phases.sh）详解

## 1. 分段定义
| 段 | 起点 | 终点 | 覆盖 |
|---|---|---|---|
| ingest | `"phase": "ingestion"` 标记 | `"phase": "qa"` 标记 | mem0(py)+milvus(go)+mock/runner(generic) |
| qa | `"phase": "qa"` 标记 | `test complete` | 同上 |

只采 replay 段，**不采 seed/index 准备段**（它们在任何阶段标记之前）。

## 2. 机制
1. `tail -n 0 -F <log>` 从末尾只读新增行；`run-single.sh` 的 tee 不带 `-a`，截断可被 `tail -F` 感知。
   **但截断感知是启发式**：GNU tail 以 `st_size < 已读偏移` 判回卷并打 "file truncated"；若 tee"打开即截断、随即写"
   且新输出**越过旧文件长度**（st_size ≥ 已读偏移），tail 无告警、只从旧偏移继续读，截断重写的前段被静默丢弃。
   **防呆**：起采集器前确保 `run.log` 不存在或为空（`rm -f run.log` 或 `: > run.log`），stale 日志 + 快速重写
   会让开头的 ingestion/phase 标记被跳过、采集器收不到。
2. 见 ingestion → 起 `collector.py --py-container mem0 --go-container milvus --container mock --container runner --detail`。
3. 见 qa → 给 ingest 采集器 SIGINT（perf-kit 捕获 KeyboardInterrupt → stop_perf 给各 perf 子进程发 INT 落盘 → presolve 符号 → 写 meta.json）→ 起 qa。
4. 见 test complete → qa SIGINT → 生成报告 → 退出。
5. 阶段切换时刻（epoch + ISO）写 `phase-markers.log`，供与 `phase_windows` 复核。

## 3. SIGINT 优雅落盘的前提
- 脚本顶部 `set -m`（开启作业控制），后台子进程 SIGINT 保持 `SIG_DFL`，Python 才能装 KeyboardInterrupt handler。
- `collect_phases.sh` 必须作为 ssh 会话**前台**命令运行，不要加 shell 级 `&`（否则子进程继承 SIG_IGN）。

## 4. 用法
```bash
# 前台起采集器
bash modules/perf-tools/collect_phases.sh <run.log> <workdir>/perf-data
# 另一会话跑 benchmark
cd modules/benchmark && bash run-single.sh 1
```
参数：1=日志路径，2=输出基目录。env：`MEM0_CTN`/`MILVUS_CTN`/`MOCK_CTN`/`RUNNER_CTN`/`MILVUS_PPROF_PORT`(留空自动)/`DURATION`(3600)/`PERF_KIT`。

**脚本来源与固定版本**：`collect_phases.sh` 出自 perf-tools 子仓（`modules/perf-tools/`），`run-single.sh` 出自
benchmark 子仓（`modules/benchmark/`），均随各自 `manifest.yaml` 钉版本；本 skill 不随包分发，拿不到时把
"已解包的 benchmark bundle + perf-tools"列为外部必需输入。**两会话靠同一日志文件耦合**：会话 A 采集器
`tail -F <workdir>/run.log`，会话 B `bash run-single.sh` 内部 `tee <workdir>/run.log`（不带 `-a`），
路径必须完全一致，否则采集器监听不到标记。

## 5. 十条容器化采集坑
1. **pprof 端口语义**：宿主 29091 = 容器内 9091；采集用 `nsenter -n` 进容器 netns，必须用 9091。留空走 socket inode 自动探测最稳（只分配给真正持有的 milvus 进程）。
2. **容器名必须逐一精确，缺容器启动前筛出**：4 个名字由 `--project-name mem0-locomo` + 服务名推导。
   当前实现 `resolve_targets` 对任一不存在容器直接 `SystemExit`。**使用前必须在启动前筛出可用目标、记录
   缺失项**：要么把「全部容器就绪」列为前置条件并显式报错列出缺失容器名；要么升级采集器为「缺失记入
   `missing_targets`、继续采其余」再降级。**不要静默跳过、也不要无声整体退出**——否则排障只看到
   "该段无 meta.json"，看不到缺了谁。
3. **缺失时该段报告无 meta.json**：缺一个容器该段直接"未找到 meta.json"，排障看 `collect.<phase>.log` 第一条报错。
   **这是"缺容器整体退出"的后果**：门禁要求"显式报错/前置条件"，与本条要一致——缺失必须显式可见，而非只剩一条日志。
4. **`--detail` Go 堆差分在 SIGINT 下缺失**：heap 起止快照差分依赖自然跑满；被打断则 end 快照不落盘。但 `gostack_<pid>.pprof` 与 `gomem_<pid>.jsonl` 完整，报告不受影响。
5. **mixed 模式两阶段墙钟重叠（归因限制）**：qa 标记在首个用户进 qa 时打印，此时其余用户仍在 ingestion。
   精确窗口以 `phase_windows` 为准，但**进程级样本无法按用户拆清**——直接切段会把 ingestion+qa 两种负载的
   样本一起写进 QA 报告。报告必须标明重叠窗口与归因限制；要纯相位数据则 `--phase ingestion`/`--phase qa`
   单相位各跑一轮，不指望 mixed 一轮给出两相位干净样本。
6. **stable_polls 行会被忽略**：seed/index 的 index-ready 等待也打印该行，但发生在 ingestion 之前。
7. **采集额外开销**：务必与计时分离；`--detail` 给 python/go 各加一路 perf。
8. **后台跑别加 `&`**：见 §3。
9. **qa 段原生帧可能全 `[unknown]`**：test complete 后容器立即销毁，presolve 时 rootfs 不可达。主数据不受影响（py-spy/pprof/gomem 都在采集期落盘）。
10. **gomem 体积巨大**：单次 ~5.7MB，16 分钟 qa 段可达 ~11.5GB（ingest 34s ~1GB）。不需 GC 时间线用 `--no-go-memstats`。

## 6. 输出结构
```
perf-data/
├── phase-markers.log
├── collect.<phase>.log
└── <phase>/{meta.json, samples.csv, interrupts.csv, perf/, metrics.json, report.html}
```
