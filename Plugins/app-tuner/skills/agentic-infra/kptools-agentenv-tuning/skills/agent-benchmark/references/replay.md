# AgentENV 默认轨迹与自选用例集

以下命令沿用 BoostKit agent-benchmark 提交 `a84b1e4` 的 preset 和 CLI。将 `BENCH_ROOT` 设为本次检出根目录，核对仓库来源、提交和工作区差异。不同版本以本次检出为准。

| 路径 | 内容 |
| --- | --- |
| `src/bench_core/` | `bench-core` 内核、回放模式与 `oversub-bench` 扫描 |
| `src/env_provider/` | `aenv`、`aenv-go`、`e2b`、`cubesandbox`、`docker`、`fake` 的 provider 实现 |
| `src/vm_monitor/` | 宿主机 Firecracker、内存、CPU 和 I/O 指标 |
| `artifacts/traces/replays/` 和 `artifacts/traces/image-map.json` | 55 条回放文件及其模板别名 |
| `artifacts/images/manifest.csv` | 每条回放对应的 ARM64 / x86_64 镜像 |
| `config/common/replay*.yaml` | 回放 preset |
| `agent_replay/` | 早期回放实现 |

## 安装和定位服务

在 Linux driver 上使用 Git、Python 3.11+ 和可写的运行目录。直接复用可用的虚拟环境。新检出可用 `git clone https://gitcode.com/boostkit/agent-benchmark.git "$BENCH_ROOT"`，安装依赖前核对来源和本轮选定的提交。下面从已安装的 `agent-benchmark` skill 目录开始，`BENCH_ROOT` 指向 driver 可读的 benchmark 仓库。若 driver 在远端，将本 skill 的 `scripts/` 放到 driver 可读的位置，并相应设置 `SKILL_DIR`。

```bash
SKILL_DIR=$(pwd -P)
cd "${BENCH_ROOT:?set BENCH_ROOT to the verified checkout}"
git log -1 --format='%h %s'
git status --short
if [ ! -x .venv/bin/bench-core ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -e .
fi
source .venv/bin/activate
bench-core --help
oversub-bench --help
```

`--provider aenv` 通过仓库依赖锁定的 E2B SDK 访问 AgentENV。若要单独测 Go 客户端，按 `go-client/README.md` 安装 Go 并执行 `bash go-client/build.sh`，使用 `--provider aenv-go`。自检时两条 CLI 均应列出用法并以 0 退出。对照实验固定 provider 和版本。需要运行仓库的 Python 测试时安装 `python -m pip install -e '.[dev]'`，用 `python -m pytest -m 'not integration'` 跑非集成用例。

- 在 AgentENV 主机记录 `uname -m`、`uname -r`、服务端版本、`/dev/kvm` 和 `ublk_drv` 的实际可用性。要用开源最新版时核对官方发布、CPU 架构、校验和与该版本的主机要求。[AgentENV Releases](https://github.com/kvcache-ai/AgentENV/releases)
- 配置 `aenv.env.E2B_API_URL` 和 `aenv.env.E2B_SANDBOX_URL` 为 driver 能访问的控制面和数据面地址。driver 位于另一台机器或另一个沙箱时，使用从其网络可达的地址。数据面走域名路由时核对 `E2B_DOMAIN`。
- 使用 `export E2B_CONFIG="/path/to/private-e2b.json"` 指向已有的私有凭据文件。字段是 `teamApiKey`（团队 API key）和 `accessToken`（服务要求时使用）。把文件权限设为 0600。准备配置的脚本会移除 `aenv.env` 中的凭据键。`aenv` CLI 的认证另用 `aenv auth`，对准同一个 endpoint 与项目。
- `vm-monitor` 在 AgentENV 主机侧才能观察到该服务的 VMM 与主机资源。从远端或沙箱内驱动时，给 driver 加 `--no-vm-monitor` 并在实际宿主机单独采集。

## 核对用例所需的模板

从本轮选中用例的 `image-map.json` 读取所需模板别名。在目标服务执行 `aenv template list --output json`，逐个核对别名、`buildStatus=ready`、`cpuCount=2` 和 `memoryMB=4096`。确认镜像架构与目标宿主机一致后可直接复用。模板大小在 `aenv pull` 时设置。

- 缺镜像时，按目标架构选择 `manifest.csv` 的 `image_arm64` 或 `image_x86`。已有 Docker 和可达 Registry 时，仓库的 `bash artifacts/images/download-images.sh` 可拉齐本机架构镜像，`python artifacts/images/build_template.py --dry-run --registry "$BENCH_REGISTRY"` 可预览全部镜像与别名。`--template <alias>` 可只选缺失项。该脚本的正式执行使用 AgentENV 的默认模板规格。需要显式 2 vCPU / 4 GiB 时，按预览执行镜像检查、tag 和 push，再用下方带规格的 `aenv pull` 为各别名建模。
- AgentENV 已能读源 Registry 或宿主机 OCI 布局时，用对应引用直接建模板。对照 cohort 的模板发生变更时记录镜像与模板版本。

```bash
# OCI_REF 为目标 AgentENV 可读的 Registry URL 或 ocidir:// 引用。
# TEMPLATE_ALIAS 取自 image-map.json。
aenv pull "$OCI_REF" --name "$TEMPLATE_ALIAS" --cpu 2 --memory 4096 --timeout 1800
aenv template list --output json
```

## 生成运行配置

从 benchmark 检出根目录运行。必填 `--api-url`。数据面地址默认与控制面相同，可用 `--sandbox-url` 和 `--domain` 指定数据面及其路由域名。准备配置无需凭据。

默认从当前提交选取仓库内置的 55 个轨迹文件名、读取工作区内容和模板映射。下面按 1536 GiB Host、1:1 生成 384 个沙箱的配置。将内存换成目标 Host 的容量。driver 与 Host 同机时可省略 `--mem-total-gib` 直接探测，异机时传 Host 的 GiB 容量或 `--mem-total-kib` 读数。

```bash
RUN_DIR=$(python "$SKILL_DIR/scripts/prepare_replay.py" \
    --api-url "${BENCH_API_URL:?set the target AgentENV API URL}" \
    --run-parent "${BENCH_RUN_PARENT:-$HOME}" --mem-total-gib 1536)
```

**从仓库挑选用例。** `--case-list` 接收 UTF-8 文本，每行一个精确的轨迹文件名，允许空行和以 `#` 开头的注释。例如，以下两条轨迹在 1:2 共用 768 个沙箱槽。

```bash
printf '%s\n' aftership__clickhouse-sql-parser-128.replay.json \
    alangpierce__sucrase-800.replay.json > "$RUN_DIR/cases.txt"
PAIR_DIR=$(python "$SKILL_DIR/scripts/prepare_replay.py" \
    --api-url "$BENCH_API_URL" --mem-total-gib 1536 --ratio 2 \
    --case-list "$RUN_DIR/cases.txt" --run-parent "${BENCH_RUN_PARENT:-$HOME}")
```

**使用自己的用例集。** 通过 `--trajectory-dir /path/to/replays --template-manifest /path/to/image-map.json` 指定目录和 JSON 对象，例如 `{"case-a.replay.json":"template-a","case-b.replay.json":"template-b"}`。脚本选取该目录下的 `*.replay.json`，也可再传 `--case-list` 选取其中一部分。轨迹内容沿用 bench-core 的 `trajectory` 格式。映射允许多个用例共用一个模板。清单为空、有重复、文件缺失或选中用例缺有效映射时，准备阶段以非零状态结束。

每次准备都会创建新的私有运行目录。脚本将选中轨迹复制到 `replays/`，将对应映射写入 `image-map.json`，并生成 `benchmark-commit.txt`、`plan.json`、`full.yaml`（固定轨迹执行）和 `lifecycle-base.yaml`（生命周期测试）。`--ratio n` 默认为 1，沙箱数为 `floor(Host 内存 GiB × n / 4)`。

## 运行固定回放

选取上一步返回的运行目录，核对模板、沙箱数和时间窗后执行。示例的 `fixed + multiplier=1` 使每个槽执行一次，`delay_scale=1.0` 保留原始轨迹间隔。压测窗口示例为 3600 秒。通过 YAML 的 `sandbox.total_count` 传递已算好的规模。

```bash
set -o pipefail
bench-core --provider aenv --config "${RUN_DIR:?select a prepared run}/full.yaml" \
    --test-duration 3600 -o "$RUN_DIR/full-results" 2>&1 | tee "$RUN_DIR/bench.log"
```

`bench-core` 按不同模板均衡分配槽位，再从对应模板的轨迹池取用例。同一轨迹可以执行多次。当槽位不足或多个模板的用例数不均时，一轮可能只覆盖部分用例。分别从索引统计唯一轨迹数和总执行次数。

预计跨 SSH 断开时，在目标机已有的持久会话或任务管理器运行，记录命令、配置路径、日志、任务 ID、启动时的 boot ID 和退出码。连接中断后先查同一任务是否还活着。

## 按需做单沙箱冒烟

首次接入或排查沙箱创建、命令执行时，指定映射到目标模板的单个轨迹文件。下面的 `delay_scale=0.02` 和 240 秒窗口仅用于冒烟。命令创建独立运行目录。

```bash
SMOKE_DIR=$(python "$SKILL_DIR/scripts/prepare_replay.py" \
    --api-url "$BENCH_API_URL" --run-parent "${BENCH_RUN_PARENT:-$HOME}" \
    --smoke-trace aftership__clickhouse-sql-parser-128.replay.json)
set -o pipefail
bench-core --provider aenv --config "$SMOKE_DIR/smoke.yaml" -n 1 \
    --test-duration 240 -o "$SMOKE_DIR/smoke-results" 2>&1 | tee "$SMOKE_DIR/bench.log"
```

异机 driver 同样需要传入本次数据面参数，并给 `bench-core` 加 `--no-vm-monitor`。

## 核验结果

1. 在本次结果目录的时间戳子目录读取 `*_run_summary.json` 和 `trajectories/index.json`。按本轮 `image-map.json` 核对唯一轨迹，按 `plan.json` 和轮数核对执行次数。用 `throughput.total/succeeded/failed`、`error` 和进程退出码确认完成情况。
2. 按 index 定位各轨迹的 `replay_result.json`，即使轨迹走完也要检查步骤异常、timeout、创建和清理错误。判读 `exit_code` 时核对原轨迹中的预期行为。`throughput.succeeded` 表示固定轨迹的步骤走完。
3. 异常退出时核对本次资源回收。`--create-only` 和 `--detect` 真后端路径有意留存沙箱，`--cleanup` 会尝试清理同一认证范围内可见的运行实例。共享服务上按此次可核对的实例 ID 处理。

更多细节见仓库 `docs/guide/bench-core-usage-zh.md` 与 `docs/guide/vm-monitor-usage-zh.md`。
