# cubesandbox-master-create-path 独立复现协议

## 1. 冻结实验身份

每次实验独立 `run_dir`，保存以下信息：

- 源码 commit、dirty diff、submodule/依赖版本，本方案及内部机制各自是否启用；
- CPU 型号/核数/NUMA、内存、内核、文件系统/挂载参数、存储、系统参数、Master 副本数、DB 配置和同机其他负载；
- 模板 ID、内容/版本摘要、READY 状态、bench commit/二进制 SHA256、完整命令、warmup、请求数/并发与时间字段；
- 各组件编译参数、工具链、产物 SHA256，部署路径及运行进程 `/proc/<pid>/exe` SHA256；
- 每个样本的开始结束时间、实例清理、残留 TAP、池水位、后台队列和实际成功数。

宿主机重启、模板/内核/文件系统或非候选配置变化后重建基线。**实验臂的代码/二进制切换是受控自变量**，允许不同哈希；不能因此禁止 A/B。组件重启必须采用相同流程并等待同样的就绪条件，不能用不同纪元的绝对值直接相减。本包自带完整环境与采样规则，无需安装其他技能。

## 2. 构建与部署

以下为附件 0.7.1 的 Linux 构建链；先读目标版本 Makefile/Cargo workspace/Go module，使用其现有锁文件，不修改依赖来迁就 patch。不要执行 git clean/reset 清工作区；保留已有修改。版本切换前保护 `CubeNet/cubevs/*_bpfel*`、`Cubelet/third_party/cubecow`、`hypervisor/Cargo.lock` 与 bench 构建产物等 gitignored 输入。

```bash
# 在目标源码根目录，先确认干净范围与当前实现
git status --short
git apply --check /absolute/path/to/scheme.patch
# 审查差异并处理本技能指出的边界后，生成当前版本候选

# Master 缓存与异步落库方案
(cd CubeMaster && CGO_ENABLED=0 make build)

(cd examples/cube-bench && go build -o bin/cube-bench .)
```

上述命令为构建，不替代各 module 的单测与目标机集成验证。CubeShim/VMM 全量构建可能数分钟，等待完成后检查退出码；不要留下持锁 cargo 进程。

本方案发布目标为 CubeMaster。由 operator 从已核验的目标发布配置中取得以下值，并以环境变量传入当前发布会话；它们是本手册的输入，不是 CubeMaster 新增的应用配置项：

| 发布会话参数 | 含义 | 文档占位符 |
|---|---|---|
| `CUBEMASTER_BIN` | 实际 CubeMaster 可执行文件绝对路径 | `<cubemaster-binary-path>` |
| `CUBEMASTER_UNIT` | systemd 管理该进程时的实际单元名 | `<cubemaster-systemd-unit>` |

以下命令仅校验输入并读取实际配置：

```bash
set -euo pipefail
: "${CUBEMASTER_BIN:?由 operator 提供 CubeMaster 可执行文件绝对路径}"
: "${CUBEMASTER_UNIT:?由 operator 提供 CubeMaster 的 systemd 单元名}"
systemctl show --property=ExecStart,MainPID -- "$CUBEMASTER_UNIT"
readlink -f -- "$CUBEMASTER_BIN"
sha256sum -- "$CUBEMASTER_BIN"
```

核对 ExecStart、目标文件与实际 Master 进程属于同一部署，再按已有发布机制执行：记录备份与哈希 → 停止对应服务/新请求 → 安装完整产物 → 启动 → 核对新 PID 的 `/proc/<pid>/exe` 与目标文件哈希 → 等心跳和模板 READY。非 systemd 部署使用实际进程管理器的等价流程，不猜测路径或单元名。真实值只保存在本次受控 `run_dir` 中，对外分享时继续使用变量名或占位符。

回退逆向安装备份并校验运行进程哈希。只操作本次授权的测试实例和服务；已有清理工具优先，不按通配符删除任意 TAP 或存储对象。

## 3. 正确性门禁

先 c1 create→list→delete；再至少 20 次正常生命周期；再执行各专项的故障与并发测试。并发修复验证可采用已有协议的 ≥6 轮连续测试，每轮完整清理，并记录实际错误数。20 次正常成功不代表取消、DB 乱序、掉电恢复均已验证。

失败请求数、成功数和请求数必须同时保留。性能比较不接受“错误请求提前返回所以更快”。异步 spec 写需等待 drain 并核对最终记录，池化需核对领养后的读写/扩容/删除和重启恢复。

## 4. 负载和时间字段

历史命令意图：

```bash
examples/cube-bench/bin/cube-bench -c 100 -n 100 -w 3 -m create-only --no-tui -o /absolute/run/report.json
```

**执行前看该版本 `--help` 确认模板/API/鉴权参数**，显式选择同一个 READY 模板，不依赖默认模板。凭证通过已有安全配置注入，不写入技能或结果报告。`-w 3` 的具体含义也按版本确认，双方一致。

附件称时间字段为 `summary.total_time_ms`，现有仓库解析器描述的 v0.7.x 格式却为 `summary.total_time_s`。脚本支持二者并统一为 ms；必须在 manifest 中指定一种，所有样本保持一致。字段缺失或两个字段不一致时拒绝分析；不根据数值大小猜单位。

主指标是一次 100 并发创建 100 个实例的整批墙钟，通常受尾部控制，但不严格等于单请求 max（排队与启动时点可能不同）。不可用 avg/std 或各阶段之和替代。时间下降 20% 对应耗时比 0.8；固定工作量下吞吐增幅是 25%，两者分开写。

## 5. ABBA 配对采样

一条比较只包含 base=A、candidate=B。正式采样前先冻结：12 个 block，每个 block 顺序 A/B/B/A；每个位置为 5 次独立 bench 调用，位置值取 5 次中位数。每次独立调用仍创建 100 个实例，不是把 5×100 合成 500 请求。每次调用之间清理并恢复准备状态，所有原始结果都保留。若成本受限可预先修改设计，但结果仅作探索，不使用本分析器的默认确认性验收。

每个 block 的 A 值是两个 A 位置中位数，B 同理；block 下降率 `100×(A−B)/A`。总估计为 block 下降率的中位数，按整个 block 重采样 10000 次、固定随机种子 0 计算 percentile bootstrap 95% 区间；另报排除平局后的双侧精确符号检验 p 值。该方法是本包定义的 `abba-block-median-v1`，不是对历史 `paired-median-sign-v2` 的实现或重算。

建议预先冻结最低实际下降阈值（示例 2%），不因结果调整。脚本 `improved` 需要区间下界超过该阈值且符号检验 p≤0.05；`regressed` 需要区间上界<0 且 p≤0.05；其余 `inconclusive`。这是单条比较的判据，多候选筛选后应对最终组合独立复验，不把筛选 p 值当多重比较校正后的证据。相邻 block 不独立或存在系统漂移时，统计区间不能补救实验设计。

池样本要记录每次调用前水位；预填满属于 warm-pool 实验，首次填充与冷池/耗尽/持续负载另报。后台 spec 的最终持久化与池补齐成本不计入 create-only 的应答时间，但必须同时交代，避免隐瞒转移的成本。

## 6. 分析器 manifest

UTF-8 JSON，路径相对 manifest 所在目录或绝对路径。示意单个 block 如下；正式输入须有 12 个连续 block，每位置列满 5 个不同报告。

可先生成完整 240 个报告路径的采集计划，避免手工填写。该命令只写新 manifest，不生成任何测量结果，也不操作服务。将下面身份值替换为本次真实值，并预先创建输出父目录：

```bash
python3 scripts/plan_comparison.py --output /absolute/run/comparison.json \
  --template actual-ready-template-id --environment actual-environment-fingerprint \
  --base-artifact actual-baseline-stack-sha256 --candidate-artifact actual-candidate-stack-sha256 \
  --metric summary.total_time_ms --minimum-reduction-pct 2
```

按 runs 数组顺序采集，每次 bench 的 `-o` 指向对应 reports 路径（先建 reports 目录）。计划默认 `correctness_passed=false`，完成对应版本正确性门禁并保存证据后才能改为 true。不要为了通过分析器提前标为 true。

```json
{
  "schema_version": 1,
  "metric": "summary.total_time_ms",
  "minimum_reduction_pct": 2,
  "workload": {"concurrency": 100, "total": 100, "mode": "create-only", "template": "ready-template-id"},
  "environment_id": "host-boot-template-config-fingerprint",
  "variants": {
    "base": {"artifact_id": "baseline-stack-sha256", "correctness_passed": true},
    "candidate": {"artifact_id": "candidate-stack-sha256", "correctness_passed": true}
  },
  "runs": [
    {"block": 0, "position": 0, "variant": "base", "environment_id": "host-boot-template-config-fingerprint", "reports": ["b0-a0-r0.json", "b0-a0-r1.json", "b0-a0-r2.json", "b0-a0-r3.json", "b0-a0-r4.json"]},
    {"block": 0, "position": 1, "variant": "candidate", "environment_id": "host-boot-template-config-fingerprint", "reports": ["b0-b1-r0.json", "b0-b1-r1.json", "b0-b1-r2.json", "b0-b1-r3.json", "b0-b1-r4.json"]},
    {"block": 0, "position": 2, "variant": "candidate", "environment_id": "host-boot-template-config-fingerprint", "reports": ["b0-b2-r0.json", "b0-b2-r1.json", "b0-b2-r2.json", "b0-b2-r3.json", "b0-b2-r4.json"]},
    {"block": 0, "position": 3, "variant": "base", "environment_id": "host-boot-template-config-fingerprint", "reports": ["b0-a3-r0.json", "b0-a3-r1.json", "b0-a3-r2.json", "b0-a3-r3.json", "b0-a3-r4.json"]}
  ]
}
```

脚本要求每份原始报告 `config` 包含上述 workload 四项（template 字段别名 `template_id` 也可），`summary.successful=100`、`errors=0`，有选定时间字段。版本输出不同则保留原始文件并做可追溯的格式适配，写明字段对应与单位，不填造缺失值。人工记录的 environment/artifact/correctness 是审计元数据，脚本不能独立证明它们真实；应链接实际采集证据。

退出码 0=分析完成（包括不显著或退化），2=格式/数据错误，绝不能把 0 直接解释为性能验收通过。`--output` 必须是新文件，避免覆盖历史结果或输入证据。

## 7. 目标机用例与最终报告

本技能 implementation.md 与 runbook.md 中的验证矩阵是 tier C 待执行清单，不伪造完成结果。报告逐项写 PASS/FAIL/NOT_RUN 和日志路径。仅比较本方案基线与候选；内部机制消融按 runbook.md 执行。历史数字用于理解原案例，不是本次通过阈值。

本包 `cases/comparison-contract` 为 tier A：仅验证分析器在合成数据、错误和边界输入下的行为；不是 CubeSandbox 编译、Agent with/without 消融或实机收益证明。

`cases/replica-schedulability` 和 `cases/spec-write-order` 需要 Go 工具链，属于 tier B。正式采集前在本技能目录执行 `python3 scripts/validate_package.py`，统一运行原始补丁格式、分析器及这两组 Go 回归；依赖缺失或任一失败均不能跳过。入口的 PASS 与第 2 节的目标构建、真实 DB/节点故障测试和实机性能结果分别记录。CI 需要显式调用该入口，不能仅凭 `case.yaml` 存在或平台合并检查绿色认定它已经运行；命令、覆盖范围和接入方式见本地 [runbook.md](runbook.md)。

完整入口还执行环境契约回归，编译运行 Go 补丁派生代码时使用专用临时 HOME/缓存及最小子进程环境，独立 runner 同样生效；这些措施不限制文件系统和网络访问。未审核代码必须放在外层无凭据隔离 runner 中执行，不能向该账户挂载真实令牌或数据库配置。仅做格式审阅时可用 `python3 scripts/validate_package.py --static-only`，该输出不代表上述执行用例已运行，也不代表补丁已通过真实源码适配检查。禁止向 runner 传入外部 `--patch` 文件，历史对照应使用独立检出的相应版本。
