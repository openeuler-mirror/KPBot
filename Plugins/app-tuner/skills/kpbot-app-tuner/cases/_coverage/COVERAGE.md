# tier-A UT 覆盖率说明（skill-ut-supplement）

> 定位：补齐此前 SKILL 开发遗漏的 UT（独立于采集替换工作）；其中验证 nmon 契约与 fcd0782 修复的用例
> 采用能力门控——master 基线全绿，collection-replacement 合入后自动激活。
> 原则：**按 SKILL 完整功能面审视覆盖率**，经验库覆盖不了的分支用合成 fixture 补齐；
> 确实无法静态测的显式列清单（不沉默）。

## 套件与结果（master 基线实测，2026-11-24）

> 运行入口：主 skill 套件在 `kpbot-app-tuner` 技能根目录执行 `bash cases/<name>/run.sh`；
> subskill 套件在对应 subskill 根目录执行 `bash cases/<name>/run.sh`；
> archive 套件在 `kpbot-profiling-advisor` 技能根目录执行 `python3 cases/archive-contract/run.py`。

| 套件 | 位置（技能根相对） | 结果 |
|---|---|---|
| detect_bottleneck（nmon 契约） | `cases/bottleneck-detection-contract/run.sh` | 0 PASS / 0 FAIL / 1 SKIP（整套门控 SKIP；collection-replacement 合入后 35 断言自动激活） |
| diagnose_environment 引擎 | `cases/diagnose-environment-contract/run.sh` | 11 PASS / 0 FAIL |
| cpu-affinity 分析链 | `subskills/cpu-affinity-optimization/cases/analysis-chain-contract/run.sh` | 11 PASS / 0 FAIL / 2 SKIP（CA-02/CA-06 门控） |
| find_optimal_queues 寻优 | `subskills/network-optimization/cases/queue-search-contract/run.sh` | 29 PASS / 0 FAIL / 1 SKIP（FQ-04 留真机 A/B） |
| BRBE 门控 + A/B 统计 | `subskills/compiler-optimization/cases/brbe-contract/run.sh` | 27 PASS / 0 FAIL |
| detect_all_libraries | `subskills/performance-library-selection/cases/library-detection-contract/run.sh` | 27 PASS / 0 FAIL |
| verify_os_change | `subskills/os-optimization/cases/verify-change-contract/run.sh` | 18 PASS / 0 FAIL |
| BIOS post_reboot_verify + BMC 凭据 | `subskills/bios-optimization/cases/post-reboot-and-credentials-contract/run.sh` | 14 PASS / 0 FAIL |
| archive_report 归档 | `kpbot-profiling-advisor/cases/archive-contract/run.py` | 11 PASS / 0 FAIL |

合计：148 PASS / 0 FAIL / 4 SKIP。

## 功能面 × 覆盖矩阵

### detect_bottleneck.sh（能力门控套件）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| 整机 CPU 饱和（均值>85） | DB-08 | 合成（经验库真机数据为未触发侧 4.42%） |
| CORE_LIST 判定（列内核均值） | DB-02 | **r1 真机受载核**（0-3 @99.8%） |
| CORE_LIST 越界不支持回落 | DB-09 | r1 真机 + 越界参数 |
| iowait>10 → disk_io | DB-04a | 合成 |
| diskbusy>80 → disk_bandwidth | DB-04b | 合成 |
| 内存高水位 → memory_capacity | DB-04c | 合成 |
| NPU cpu_feed / compute / other / unavailable | DB-10/11/12/13 | 合成 utilization 表（r7 归档为 `-t usages` 键值格式不可直接复用）+ 合成 nmon |
| parse_host_cpu mpstat.txt 回退（n=1 均值语义） | DB-14 | **r1 真机 mpstat**（2.86→3） |
| 降级三联（nmon/proc/sock 不可用） | DB-03 | stub PATH（无 nmon/pidstat/ss/python3） |
| 契约值逐字段（cpu/idle/iowait/disk/memory） | DB-01 | **r1 真机 nmon** |
| 网络主分支（丢包/队列）+ 次分支（pps×irq） | DB-05 SKIP | **待 C3**：需 socket-queues/net-bandwidth/irqstat-window 快照输入 + B4 `--irqstat` |
| needs_collection 契约 | DB-06 SKIP | **待 C3**：纯化后缺失输入的结构化输出 |
| 容器检测 | DB-01 宽松断言 | 宿主相关（count>0） |

### diagnose_environment.py 检查引擎

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| reference-issues 三态（passed/failed/degraded + blocked_items 高严重度） | DE-01 | 合成 issues.json（7 项混合检查集） |
| check_one 原语（backup_file_contains/not_contains、file_equals、file_exists、command_contains） | DE-01 | 合成 fixture |
| 不支持检查类型 → unknown 进 degraded_items | DE-01 (v7) | 合成 |
| `_invalid` 标记 → invalid 状态 | DE-02 | 合成 |
| 缺失 reference-issues 路径 → not_present | DE-03 | 合成 |
| 缺必填参数 → argparse 非零退出 | DE-04 | — |

### cpu-affinity 分析链（aggregate + generate）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| skewed → rebalance（双格式） | CA-01/02 | **A/B 真实诊断数据**（pretty=旧链 / compact=SKILL 路径，CA-02 门控待 fcd0782） |
| migration high → reduce | CA-03 | A/B 数据 + 注入 |
| migration medium/low → preserve | CA-03 | 同上 |
| skewed+high 优先级（high 覆盖） | CA-08 | A/B 数据 + 注入 |
| reduce 的 next_round 契约 | CA-03 | 同上 |
| 缺文件默认值注入 | CA-04/05 | A/B 数据裁剪 |
| 指引契约锁定（三区间阈值文本） | CA-06 SKIP | SKILL.md（指引补全合入后激活） |
| 缺 summary 优雅降级 | CA-07 | — |

### find_optimal_queues.sh（network-optimization）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| 缺必填参数 → exit 1 | FQ-01 | — |
| get_max_combined 解析（`ethtool -l` Pre-set maximums） | FQ-02 | mock ethtool |
| 饱和首发 → ADD → 等 QPS 收敛 → best_q=31 | FQ-02 | mock mpstat（sat 命中）+ echo 型 bench |
| Phase2 末次 `-L` 恢复最优队列数 + 恢复前队列数核验（ethtool -l 对照 best_q，失败拒绝最终压测） | FQ-02 | 有状态 ethtool mock（-l 反映末次 -L） |
| taskset stdout 污染拦截（stdout 契约） | FQ-02 | mock pgrep 输出 PID + taskset 模拟真机 stdout（锁 PR#194 修复） |
| Phase2 恢复获胜轮应用绑核（app_start=IRQ_BASE+best_q，防末轮残留范围与 IRQ 核重叠） | FQ-02 | taskset mock 落盘参数，断言恢复调用（锁 mxj1314 检视 #3） |
| Phase2 应用绑核恢复的读回核验（/proc 判存活 + 行尾掩码提取 + `\|\| true` 防 set -e 吞真实失败，locale 无关；存活不可读/未落获胜范围 → 拒绝最终压测） | FQ-02/07/08/09 | 双模式 taskset mock（掩码状态）；FQ-07 注入 set 静默失效、FQ-08 注入存活读回 rc≠0（真实 ESRCH 失败签名，断言走到 ERROR 门控）、FQ-09 非英文文案（锁 mxj1314 检视 #3 + 两轮复核 + huangsibeier 检视） |
| 已测队列数防重复守卫（state_history 落盘——子 shell 数组对主 shell 不可见的第三处；方向翻转复访被拦截） | FQ-10 | 有状态 mock：递增 QPS 的 bench + 交替饱和度的 mpstat 驱动 ADD/REDUCE 翻转，确定性构造复访（旧数组实现在同场景下守卫 0 触发、跑满 MAX_ITER 空转——已对抗验证） |
| 外部命令 stdout 泄漏进样本通道 → 整行契约拒收 → 停止寻优（不进 Phase2，防零值伪收敛） | FQ-05 | 会打印 Removed 行的 systemctl mock（锁 mxj1314 检视 #2） |
| 伪数值样本（qps='.' 可过宽松正则）→ 严格数值契约拒收 | FQ-06 | bench 输出 `(. per sec.)` 使 qps 提取为 '.'，断言 `\d+(\.\d+)?` 拒收（锁 mxj1314 复核 #2） |
| 不饱和首发 → REDUCE | FQ-03 | mock mpstat（idle 95%） |
| RSS 塌缩（ratio>50 → STOP:rss_collapse） | FQ-04 SKIP | /proc/interrupts 前后差分读绝对路径不可 mock；真机 A/B（journal r4 已验证） |

### brbe_env_check.sh + brbe_ab_benchmark.sh（compiler-optimization）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| --workdir 注入白名单（元字符拒绝） | BE-01 | — |
| mock 全绿 → ready + JSON 契约（cpu_arch HiSilicon / npu davinci 判据 / brbe_hw 真实分支对≥20 / disk df） | BE-02 | mock bin（含 df，与宿主磁盘解耦） |
| 无 NPU → blocked + next_steps（KPBOT_SOURCE_CANN=0 关闭深度探测；PATH 仅 mock + coreutils 白名单 stub，宿主 /usr/bin//bin 整体排除，任何宿主结果一致） | BE-03 | mock bin + STUB 白名单符号链接（锁 mxj1314 检视 #4 + 复核 #1） |
| --container 模式：宿主 KPBOT_SOURCE_CANN 显式经 `docker exec -e` 传入容器探测命令（默认 0） | BE-04 | docker mock（记录调用参数 + canned 探测输出，锁 mxj1314 检视 #1） |
| A/B run 统计（mean/cv/values） | AB-01 | 固定值 3 轮 |
| 指标提取失败 → exit 1 | AB-02 | — |
| --label 路径逃逸白名单 | AB-03 | — |
| compare 四态判定（accept/reject/below_min_gain/regression） | AB-04a-d | 合成 JSON |
| 噪声公式回归（sqrt(a²+b²)） | AB-04e | 合成 JSON |

### detect_all_libraries.sh（performance-library-selection）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| perf report 文本格式（含真实零误报） | PL-01/01b | **r2 真机 perf 基线** + 合成 |
| symbol JSON（桥接契约 format 1） | PL-02 | 2026-11-11 桥接验证实测数据 |
| devkit 原生表格（识别边界负例） | PL-03 | **r2 真机 devkit 报告** |
| evidence_report_json（引用提取+静态） | PL-04 | 合成 |
| 18 类别动态检测 | PL-01b/02/06 | 5 类真机/实测 + 13 类合成（经验库仅覆盖 libc/memset 场景） |
| ascend_runtime 三态（强竞争/current/弱竞争/blocked） | PL-02/07/05 | 实测 + 合成 |
| structured_json_no_symbol 递归提取 | 未测 | 低价值路径（format 1 已覆盖主链） |

### verify_os_change.sh（os-optimization）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| sysbench tps 增益超阈值 → verified_effective / KEEP + verification_result.json（gain_pct/verified） | VO-01 | echo 型 bench 命令 |
| 增益低于阈值 → below_threshold / ASK USER（仅提示） | VO-02 | echo 型 bench 命令 |
| 负增益 → harmful / AUTO ROLLBACK 且回滚命令被执行（marker 证明） | VO-03 | rollback-cmd touch marker |
| baseline-only 模式 → 基线后退出、不产生决策 JSON | VO-04 | — |
| 指标提取——pgbench / redis-benchmark / custom 正则三类 | VO-05 | echo 型 bench 命令 |
| 指标提取失败 → 基线阶段 exit 1 | VO-06 | — |
| 缺必填参数 → exit 1 | VO-07 | — |

### post_reboot_verify.sh + collect_bmc_credentials.sh（bios-optimization）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| 无参数 → usage 退出 | PR-01 | — |
| 快照文件不存在 → exit 1 | PR-02 | — |
| 无采集器参数（power_profile 等）→ 需 Redfish 确认路径 + 汇总声明 | PR-03 | 合成 snapshot |
| 有采集器参数（smt）→ 逐行输出当前值并完成比对 | PR-04 | 合成 snapshot + 宿主 /sys 只读（缺失时降级 N/A，断言仅查行存在） |
| collect_bmc_credentials --help / 缺 --output-dir 校验 | BMC-01/02 | — |
| 凭据经环境变量传递给 backup 脚本（mock 接收器验证 args/env/长度） | BMC-03 | 合成 backup 接收器 |
| 密码不落盘（全部产物扫描，含 output-dir 内容） | BMC-03 | — |

### archive_report.py（kpbot-profiling-advisor）

| 功能分支 | 用例 | 数据来源 |
|---|---|---|
| ZIP 归档产出 + 图片引用重写为随包附件 + SHA256 清单 | AR-01 | 合成报告目录 |
| sha256 与成员内容一致 | AR-07 | 合成 |
| 不同目录同名附件去重（-2 后缀） | AR-02 | 合成 |
| Windows 保留名（CON → _CON） | AR-03 | 合成 |
| --attach 附件清单回写报告 | AR-06 | 合成 |
| 输出非 .zip 后缀 / 引用缺失文件 → exit 1 | AR-04/05 | — |

## 显式未覆盖清单（含解锁条件）

| # | 项 | 原因 | 解锁条件 |
|---|---|---|---|
| 1 | 网络判定主/次分支（detect_bottleneck） | 依赖 live 窗口（sock/pps/irq），无静态输入 | C3 纯化（快照输入）+ Phase B4 os-collector `--irqstat` |
| 2 | needs_collection 契约（detect_bottleneck） | C3 引入的新输出 | C3 |
| 3 | live nmon/pidstat/sock 内部路径（detect_bottleneck） | C3 的移除对象 | 不补测（删除） |
| 4 | find_optimal_queues RSS 塌缩分支（FQ-04） | /proc/interrupts 前后差分读绝对路径不可 mock | 真机 A/B（journal r4 已验证） |
| 5 | analyze_system_topology | 就地 lscpu/sysfs，C4 改快照输入后才有稳定契约 | C4 |
| 6 | verify/apply/rollback 执行脚本 | 执行与验证类目（三类分界），不属分析 UT | 范围外（设计 §2）；os-optimization 的 verify_os_change 契约已由 VO 套件覆盖（三态决策/回滚判定/JSON 契约均为纯函数断言） |
| 7 | agent 级规则（SKILL.md 阈值/门控） | D1 决策：不脚本化 | fixture 沉淀于 `cases/_coverage/agent-rules/`，归后续 evals |
| 8 | irq 降级（/proc/stat 不可读） | 内核接口无法在 UT 中屏蔽 | 范围外 |
