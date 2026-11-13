# Master 缓存与异步落库操作手册

## 1. 明确数据与契约

准备源码、Master 副本数、DB、模板 ID 与 READY 副本列表、spec 消费方、基线版本和独立 `RUN_DIR`。确认成功应答是否要求 spec 已持久化；不允许延迟时只实施缓存分支。记录允许的缓存陈旧时间，多 Master 不能默认立即一致；未实现数据库版本条件或分布式 fence 的多写者部署不启用异步 spec。

```bash
set -euo pipefail
cd "$SOURCE_ROOT"
mkdir -p "$RUN_DIR/source" "$RUN_DIR/build" "$RUN_DIR/reports"
git rev-parse HEAD > "$RUN_DIR/source/commit.txt"
git diff --binary > "$RUN_DIR/source/preexisting.patch"
rg -n 'ResolveTemplateReadyReplica|isTemplateReplicaSchedulable|skipTemplateLocalityReady|configureSandboxSpecHooks|func Put|func Delete|func Init' CubeMaster/pkg
git apply --numstat "$SKILL_DIR/references/scheme2-rsi-cache.patch"
git apply --stat "$SKILL_DIR/references/scheme2-rsi-cache.patch"
git apply --check "$SKILL_DIR/references/scheme2-rsi-cache.patch"
```

`SOURCE_ROOT`、`SKILL_DIR` 和 `RUN_DIR` 均为实际绝对路径。六个文件的对应关系见 implementation.md；已有缓存或异步化则先核对键和失效覆盖，避免叠加不同 TTL 层。

以下检查逐层收敛结论，不互相替代：

| 检查 | 能确认的范围 | 后续检查 |
|---|---|---|
| `git apply --numstat` / `--stat` | 原始 patch 的结构可被 Git 解析 | 在目标源码树上检查上下文 |
| `git apply --check` | 当前源码上下文允许应用，不修改源码 | 应用或迁移后运行编译与相关测试 |
| 包内 Go 回归 | 参考缓存/选择与 spec 写入顺序代码在 fixture/stub 下的行为 | 目标 DB、健康状态源与生命周期集成 |
| 目标构建和集成 | 本次实际组件与依赖的兼容性 | 同条件创建时间与后台完成情况的比较 |

计数以原始 patch 文件为准：hunk 内空格开头的行同时计入旧侧和新侧，`-` 计入旧侧，`+` 计入新侧。评审页面外层“新增整个 patch 文件”的标记不参与内部 hunk 计数。出现解析错误时保留完整命令、Git 版本和 stderr，先核对原文及换行格式，再定位具体 hunk。

本技能的 `.gitattributes` 将 `references/*.patch` 固定为 LF。统一入口直接读取交付文件的字节，拒绝 BOM 和 CRLF，不先归一化再宣称原文件通过；这样与上面 Git 直接读取文件的路径保持一致。Windows 既有工作区若仍保留旧 CRLF 文件，应在保留本地改动后，从同一提交重新检出或导出原始 LF 补丁，不靠手改 hunk 头解决换行问题。

## 2. 建立观测基线

围绕一个创建请求记录 locality 判定、ready-list 解析/bind、skip-locality 查询、spec 编码和 UPSERT 起止。以 sandbox ID/request ID 关联调用，记录同一个 template/node 键被读取次数。SQL 计数只记录指纹与耗时，不输出凭证或业务 spec 内容。

若是独立测试 DB，可在突发前后查询以下只读状态；共享 DB 必须扣除其他业务影响或改用调用点计数：

```sql
SHOW GLOBAL STATUS LIKE 'Com_select';
SHOW GLOBAL STATUS LIKE 'Com_insert';
SHOW GLOBAL STATUS LIKE 'Com_update';
```

INSERT/UPDATE 的变化受 UPSERT 和并发更新影响，不能单靠一个全局计数断言 spec 全部写入。应对本轮 sandbox ID 列表逐个核对最终记录及删除结果。

## 3. 分支 A：模板解析缓存

1. 列出查询→缓存键→值→写路径失效的对应表。历史 schedulable 键为 `templateID + NUL + nodeID`，ready-list 为 `templateID + NUL + ready`。当前系统存在租户/版本隔离时补足语义维度。
2. ready-list 仅缓存 READY 候选元数据，不缓存单次选择；填充列表时不按请求的动态健康状态删掉候选。单副本缓存保存 `ReplicaStatus`，不保存可调度 `bool`。两类缓存命中后都用本次请求的 `ctx` 执行 `isReplicaSchedulableNow`，再做 preferred-node 选择。检查目标实现是否包含真实健康查询：0.7.1 只有 READY 判定，不能把函数名当作节点健康保证。
3. Lookup 在缓存锁内取得 generation，singleflight 键包含该代；在途 miss 使用无值占位。查询只对当前代条件回填，失败也只释放自己的占位；每个 waiter 醒来后再次检查代际，过代结果或错误检查 ctx 后重试。返回不可变值或复制，单 follower 的取消行为另按目标请求契约验证。
4. 1s TTL 从成功回填开始。写后失效与条件 Store/Discard 使用同一缓存锁，新请求不加入旧代 flight。按下表用 barrier 验证在途交错；skip-locality 缓存与 templatecenter 不同包，单独核对失效入口。
5. 区分 NotFound 与 DB 暂态错误；无 READY 不做阻塞新状态的负缓存。模板 kind 不能证明排他时不要直接删除查询。

先仅编译并验证该分支，再与 base 做 c100 比较，记录命中、miss 合并数与 SELECT。

对同一键先产生一次 miss，再产生 hit，分别记录元数据读取与当前判定的调用次数：缓存命中应减少元数据 DB 读，当前判定仍随请求发生。再固定 preferred node 并改变目标判定源的状态，确认不会因共享命中绕过当前请求的筛选；v0.7.1 本身没有节点健康查询，真实健康行为应在目标实现接入后验证。

## 4. 分支 B：异步 spec

记录构造 canonical record 与 DB 写入的分界。行构造仍在请求内，入队的是独立不可变记录；不要在 worker 中引用调用者仍会修改的 req/map。

512 队列/2 worker/最多 3 次尝试是本包参数起点。worker 取出队列项开始处理时创建 10s context，包含 gate 等待、UPSERT 与退避，不含此前排队时间。Put、Delete 和 PutAsync 等待 gate 服从本次请求 ctx；PutAsync 不保证同 ID 争用时零等待。队列满在同 ID gate 内回退同步写，store 未就绪保留显式错误和计数；不能把队列非空等同于数据已持久化。观测队列深度、最老待写年龄、gate 等待、成功/失败/重试及关闭 drain。

修订补丁让 Put、异步入队/回退、Delete 和 worker UPSERT 共用按 ID 的可取消 gate，并以 generation 跳过过代队列项。实际写入和有限重试持有 gate，新写入/删除等待在途旧写结束后再落地；状态在所有排队、等待和在途引用释放后回收。迁移时核对所有 spec 写者都经过该协议，再用故障屏障验证下表交错。该修复限于单进程顺序；真实 DB、多 Master 及崩溃恢复仍需目标集成，best-effort 的丢失窗口与消费方接受条件单独记录。

generation 在取得 gate 并开始新操作时推进。新 Put、Delete 或队列满同步回退若随后写库失败，也不会重新放行更旧的队列版本；这表示最新已接受意图优先，调用方须处理错误并重试新操作，而不是退回“最后成功写胜出”。等待 gate 时被取消的请求尚未推进 generation，不会撤销旧任务。

## 5. 确定性故障测试

使用 DB stub 的 barrier/channel 阻塞指定 UPSERT/SELECT，控制顺序；sleep 只可辅助，不作为竞态覆盖证据。

| 安排 | 预期 |
|---|---|
| 阻塞旧 SELECT→更新副本并失效→释放旧读 | 不能把已失效 generation 回填为新缓存 |
| READY 元数据命中→实时健康状态变化→恢复 | 目标实时判定每请求重查；不缓存健康结论，恢复不需等待元数据 TTL |
| 副本删除/READY 状态更新→写后失效 | 下一请求读取新元数据；跨 Master 按已声明的 TTL/失效契约验证 |
| leader 出错、follower 取消 | 核对原 Do 共享查询/错误的实际边界；独立 waiter 即时取消隔离不在本次修复范围 |
| 先无 READY，再注册 READY | 新副本及时可用 |
| 阻塞 create(v1) UPSERT→完成 Put(v2)→释放 v1 | 最终仍为 v2 |
| 阻塞 create UPSERT→Delete→释放旧写 | 最终无 spec 行，无复活 |
| 队列满/DB 超时/关闭/重启 | 回退和丢失窗口符合契约，失败可观测 |

对目标版本运行相关包单测，支持时再用 `go test -race` 覆盖改动包。race 检测器不能证明数据库顺序正确，仍需上述断言。

### 统一校验入口与覆盖范围

在本技能目录执行（Git、Python 3、Go 1.20+；Go case 无外部 module 依赖）：

```bash
python3 scripts/validate_package.py
```

工具不在 PATH 时可使用 `--go /absolute/path/to/go`、`--git /absolute/path/to/git` 显式指定。入口依次执行以下检查；缺工具、缺入口或任一步失败均非零退出，只有全部执行成功才输出最终 PASS：

| 阶段 | 执行内容 | 能证明的范围 |
|---|---|---|
| 原始补丁 | UTF-8/LF 字节、SHA256、逐 hunk 上下文/删除/新增计数，Git `--numstat`/`--stat` | 随包原文件格式与内部行数可复核，不做隐式换行修正 |
| tier B 环境契约 | `cases/environment-contract/test_environment.py`（需要 Git） | 以假凭据 sentinel 验证实际子进程不继承父进程令牌、配置和 PATH，以及静态模式/外部补丁参数的边界 |
| tier A 分析器 | `cases/comparison-contract/test_compare.py` | 合成输入、配对计算、格式错误和输出保护等工具行为 |
| tier B 缓存 | `cases/replica-schedulability/run.py` | 实际参考缓存/选择代码在受控 DB、请求策略与时序下的行为 |
| tier B 写入顺序 | `cases/spec-write-order/run.py` | 实际参考 spec 写入代码在受控持久化与并发安排下的行为 |

缓存 runner 将参考补丁的实际 hunk 应用到随包 v0.7.1 片段后重新读取、提取函数，再编译执行；DB/model 与健康状态源是 stub。健康 stub 在 v0.7.1 的 READY 判定之上加入动态状态和请求策略，用于验证目标扩展的判定不会被缓存绕过，不代表 v0.7.1 本身具有节点健康查询。具体用例名与数量以本次 Go 输出为准，报告保留入口命令、补丁 SHA256、工具链版本和各阶段退出码。

写入顺序 runner 编译实际 `async.go` 和应用补丁后的 Put/Delete，以受控队列和屏障调度真实持久化函数，DB、model 与请求编码使用 stub。它验证同 ID 的顺序、过代、错误和取消边界；没有验收自动 worker 池启动、服务关闭 drain 或真实后台吞吐。不同 ID 的 gate 独立，固定 worker 池本身仍可能因繁忙任务发生排队。

环境契约使用专用的假值，不读取或记录真实凭据；两个 Go harness 也检查常见令牌变量不可见。验证环境清理时，应给外层调用注入这些假值并保留测试输出，而不是将真实 CI 令牌用于测试。2026-11-13 本地在 Windows/amd64、Go 1.23.12 上向外层进程注入假 CI/AWS/DSN/sentinel 变量后，完整入口通过 4 项环境契约、8 项分析器、19 项缓存及 15 项写序测试；实际编译执行的 Go 测试也确认未继承这些变量。该记录只覆盖本次包内回归，不代表文件系统/网络隔离或目标集成已验收。

统一入口不需要完整 CubeSandbox 源码树，因此不替代第 1 节在真实目标源码上的 `git apply --check`。参考基线为公开 `TencentCloud/CubeSandbox` 的 `v0.7.1`（`31d911e430fdf8a8879bd062b8e78066c1a8e89d`）；迁移到其他提交时重新检查上下文并执行完整构建。该版本 `CubeMaster/go.mod` 要求 Go 1.25.7，生产构建遵循目标 module，不能用隔离回归测试的最低 Go 版本覆盖其要求。完整 Master 构建、真实 DB/节点集成和性能测量分别记录，统一入口的 PASS 只表示表中各项检查已执行成功。

### 执行环境与只读静态模式

完整入口会编译并执行由随包补丁和 fixture 生成的 Go 代码，比较器用例也会运行 Python 子进程。统一入口、两个独立 Go runner 和比较器子进程共用本包的环境构造规则：从空映射建立白名单，只保留必要 OS 支持值、显式工具目录和系统目录组成的 PATH，配置专用临时 HOME/USERPROFILE、XDG 目录、GOCACHE、GOPATH、TMP；父进程的令牌、数据库 DSN、代理、PYTHONPATH、GOFLAGS 和 Git 配置不透传。独立调用同样执行这套清理，不能绕过聚合入口重新暴露环境。

这些措施限制环境继承和默认配置查找，不限制进程读取其账户权限下的其他文件，也不阻止测试代码访问网络；`GOPROXY=off` 只阻止模块代理下载。入口脚本自身也是仓库中的可执行代码，运行未审核 PR 前仍应在外层 CI 使用无凭据账户/隔离 runner，并移除凭据挂载；不能把本脚本当作对恶意仓库代码的沙箱。需要禁网和只读文件系统时，由实际 CI/容器配置落实。

只需审阅补丁格式时，在本技能目录执行：

```bash
python3 scripts/validate_package.py --static-only
```

此模式只需 Python 和 Git，执行原始字节/hunk 统计及 Git 解析，不运行分析器、Go 或 patch 派生代码，不做真实源码树 `git apply --check`。日志明确输出 STATIC PASS，不能将其登记为完整包内回归或性能通过。runner 不再支持指向任意外部文件的 `--patch`；历史版本对照应在相应版本的独立、无凭据检出环境中运行，不把外部 patch 交给本入口执行。

### 接入实际 CI

已核对的平台 `check_build` 仅执行仓库拉取与合并检查，没有自动运行这些 case；外部 Jenkins 任务配置不在本仓库中。新增 `case.yaml` 或本入口本身不会使该任务自动执行 Go 测试。维护者需先审查入口及被执行内容，在已安装 Git、Python 3 和 Go 的无凭据测试 stage 中，从仓库根调用：

```bash
python3 Plugins/code-optimizer/skills/cubesandbox-master-create-path/scripts/validate_package.py
```

接入后以 stage 中实际出现的命令、最终退出码和测试输出确认执行。记录时分别列出“平台合并检查”“包内统一校验”“目标构建/集成”和“性能测量”，不能因平台标签为绿色就将后面几项标记为 PASS。

## 6. 构建、发布、比较

```bash
set -euo pipefail
cd "$SOURCE_ROOT/CubeMaster"
CGO_ENABLED=0 make build > "$RUN_DIR/build/master.log" 2>&1
```

成功后记录产物与原二进制哈希，用目标已有发布机制停 Master、安装完整产物、启动。核对新进程哈希、心跳及模板 READY；历史环境心跳恢复约 130s，当前以状态为准，不能只 sleep 固定秒数。

先 20 次生命周期，再测 base/cache-only/async-only/cache+async。每轮清理后验证 spec 删除，正式测量后等待 worker drain，保留最终行数。创建快路径变快但持久化欠账不断增长时，不宣称持续吞吐改善。具体采样与分析命令见本地 [reproduction.md](reproduction.md)。

四组使用相同模板、请求数、Master/DB 拓扑和日志等级，分别记录实际启用机制。`total_time` 下降率按 `100 × (base − candidate) / base` 计算；SELECT 减少用于解释缓存贡献，drain 后的最终记录用于确认异步完成。不能把创建应答时间、后台 drain 时间和 SQL 数量混成一个收益指标。

## 7. 交付与回退

交付缓存键/失效表、异步状态顺序、代码版本/哈希、DB 计数、正确性结果和 total_time 下降率。缓存与异步分支分别可撤回；关闭/回退前确认待写处理策略。恢复备份 Master 后核对运行哈希与 READY，不能只验证文件已复制。所有工具与原 patch 均在本目录，可独立复制执行。
