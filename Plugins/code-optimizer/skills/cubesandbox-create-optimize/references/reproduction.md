# CubeSandbox 创建性能复现协议

## 1. 冻结实验身份

每次实验独立 `run_dir`，保存以下信息：

- 源码 commit、dirty diff、submodule/依赖版本，三个候选各自是否启用；
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

# 方案一：按目标 workspace 确认 VMM 会被同一次构建包含
(cd CubeShim && cargo build --release --locked)

# 方案二
(cd CubeMaster && CGO_ENABLED=0 make build)

# 方案三：先 Rust 库与头，再 Go cgo
(cd cubecow && cargo build --release --locked)
mkdir -p Cubelet/third_party/cubecow/lib Cubelet/third_party/cubecow/include
cp cubecow/target/release/libcubecow.a Cubelet/third_party/cubecow/lib/
cp cubecow/include/cubecow.h Cubelet/third_party/cubecow/include/
(cd Cubelet && CGO_ENABLED=1 go build ./... && CGO_ENABLED=1 go build -o cubelet ./cmd/cubelet)

(cd examples/cube-bench && go build -o bin/cube-bench .)
```

上述命令为构建，不替代各 module 的单测与目标机集成验证。CubeShim/VMM 全量构建可能数分钟，等待完成后检查退出码；不要留下持锁 cargo 进程。

部署使用目标已有发布机制：记录备份与哈希 → 停目标服务/停止新请求 → 完整安装产物 → 启动 → 检查运行哈希 → 等心跳/模板/池就绪。由 operator 先核对实际部署配置，通过运行时环境变量或发布参数提供 `MASTER_BIN_DIR`、`CUBELET_BIN_DIR`、`SHIM_BINARY_PATH`、`MASTER_SERVICE_UNIT` 和 `CUBELET_SERVICE_UNIT`；本包不固化安装路径或单元名。不要覆盖正在运行的可执行文件来代替停服发布。

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

各专项实施说明中的验证矩阵是 tier C 待执行清单，不伪造完成结果。报告逐项写 PASS/FAIL/NOT_RUN 和日志路径。组合至少包括 base/1/2/3/123；完整 2³ 消融含 12/13/23。同一源码基线和编译参数下测量，组合总下降率独立计算，无法匹配历史 15–20% 时如实报告差异及已确认的环境因素。

本包 `cases/comparison-contract` 为 tier A：仅验证分析器在合成数据、错误和边界输入下的行为；不是 CubeSandbox 编译、Agent with/without 消融或实机收益证明。

`cases/preconnect-lifecycle` 为 tier B：执行 `python3 cases/preconnect-lifecycle/run.py`。复现环境使用 Rust/Cargo 1.85.1 及 Python 3.9+；Cargo.lock 锁定 Tokio 1.44.2 和依赖，首次运行需要受控 registry 访问，缓存齐全后可离线执行。runner 把本包 patch 中的实际 setup/join 区域嵌入 mock VM/传输 harness，在真实 Tokio runtime 中验证失败和取消后的资源回收、重试停止、deadline、串行回退和 c100 失败突发；生成源码副本位于该 case 的 `results/`，不入库；实际编译项目、HOME、Cargo 配置与 target 在独立临时目录中，执行后清理。该用例通过不表示完整 CubeShim/Guest 集成与目标机性能已复测。

锁文件应由真实 Cargo 解析维护。Tokio 是本用例的依赖而非 workspace member，本 manifest 仅启用 `rt`、`macros`、`time`、`sync` 和 `test-util`；未启用的 `net`、`io-util`、`process` 等特性，其可选依赖不要求出现在锁文件中。另一方面，非可选的 target/cfg 依赖会跨平台解析，因此 `backtrace` 或其 Windows 依赖出现并不意味着必须把 Tokio 所有可选依赖补齐。规则见 [Cargo 依赖解析](https://doc.rust-lang.org/cargo/reference/resolver.html#features)。变更 manifest 后用 Cargo 重新生成并审查锁文件，不手工裁剪；实际以 `cargo test --locked` 的退出码、测试日志及执行前后 Cargo.lock 哈希判断是否通过，不由包名列表推断结果。

`cases/replica-schedulability` 与 `cases/spec-write-order` 为 tier B：分别运行 `python3 cases/replica-schedulability/run.py` 和 `python3 cases/spec-write-order/run.py`，需要 Git、Go 和 Python。它们应用或提取本包 S2 参考补丁，在可控 DB fixture 上验证缓存失效代际和同 ID 写入顺序；真实数据库、多 Master、外部写者与进程关闭 drain 仍是集成验证项目。

所有 tier B runner 都会编译并执行参考补丁中的代码。使用不含云、CI、镜像仓库或数据库凭据的隔离测试环境；最小环境变量和离线依赖仅收敛进程输入，不能隔离相同操作系统账号能访问的文件或阻止任意网络访问。CI 应使用无凭据的临时执行器，按需要另行限制文件系统和网络。平台门禁绿色不等于这些用例已实际执行，记录各命令的真实退出码和日志。

## 8. 统一校验与锁定依赖复核

在本技能目录执行 `python3 scripts/validate_package.py`，完整入口要求 Git、Go、Rust/Cargo 和 Python；缺依赖或任一步失败均返回非零。仅审补丁格式时执行 `python3 scripts/validate_package.py --static-only`，只检查三个随包补丁的原始字节、hunk 计数和 Git 解析，不编译、不运行 case，也不代替目标源码树的 `git apply --check`。

Cargo runner 只读取本包 manifest/lock，不继承父进程的令牌、RUSTFLAGS、Cargo 配置或 registry 凭据；正常 Rustup 安装先用可信的 `rustup which` 定位真实工具链，再在测试环境中使用该路径。需要时显式指定 `--cargo /path/to/cargo`、`--rustc /path/to/rustc`。构建机应备有与目标匹配的链接器；便携工具链可用 `--linker /path/to/linker`，Rust GNU 自带目标库需要时再加 `--link-self-contained`，不把本机绝对路径写入 skill。

需要可重复的离线验证时，在无凭据的受控准备环境中使用 Cargo 生成并审查与本锁文件匹配的 vendor 目录，随后执行：

```bash
python3 scripts/validate_package.py --offline --vendor-directory /path/to/cargo-vendor
```

该参数只在临时 Cargo home 写入 source replacement，不加载 vendor 目录外的用户 Cargo 配置；目录中的源码仍会被执行，必须按普通依赖审查。未提供 vendor 时使用新的临时 Cargo home 访问公开 registry，不复用操作者的凭据缓存。临时工作目录的祖先存在 `.cargo/config` 或 `.cargo/config.toml` 时拒绝执行，应选择无此配置的临时目录。

2026-11-13 使用官方 Rust/Cargo 1.85.1 在 Windows/amd64 对原有 manifest 和锁文件执行 `cargo test --locked --offline`，锁定的 18 个 registry 包逐一按 Cargo.lock 校验和获取，原有 12 项生命周期测试全部通过，锁文件执行前后字节不变。该结果否定“缺少未启用的 Tokio 可选依赖必然使 --locked 失败”的推断；没有手工增删依赖来迎合包名计数。同日向外层进程注入合成凭据变量后执行新版完整入口，退出码为 0：4 项环境契约、8 项分析器、19 项缓存、15 项 spec 写序与 13 项 Rust 测试全部通过，共 59 项；Rust 项包含原有 12 项生命周期和 1 项实际执行进程的环境断言，锁文件仍未变化。
