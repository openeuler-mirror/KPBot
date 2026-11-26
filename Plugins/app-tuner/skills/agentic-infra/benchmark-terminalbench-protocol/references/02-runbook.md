# TerminalBench 回放压测 Runbook

> 命令契约以 suite（commit `673be52`）`benchmark/agent_replay/README.md` 与
> `builder/trajectory_importer/README.md` 为真源；本文只固化 terminalbench 路径与差异点。

## 1. 环境准备

前提：真源为个人仓，需先获得仓库访问授权（未授权账号 403），后续迁移公开托管后修订引用。

```bash
git clone https://gitcode.com/linhandy/agent-workload-benchmark-suite.git && cd agent-workload-benchmark-suite
git checkout 673be52                      # 基准 commit（DOMAIN.md 真源声明）
uv sync --locked && source .venv/bin/activate    # pip/venv 方式见 suite README

# 后端预检（按选用后端执行其一）
docker info > /dev/null && pip install docker    # docker 后端
# aenv/e2b 后端：runner check 服务检查；凭证从已配置环境或密钥管理器注入，或用 runner
# 的 --api-key-file 指向密钥文件；确需手工输入时用 read -rs（避免凭据进入 shell 历史）：
export E2B_API_URL='<AgentENV API URL>'
export E2B_SANDBOX_URL="${E2B_SANDBOX_URL:-$E2B_API_URL}"
read -rs E2B_API_KEY && export E2B_API_KEY
read -rs E2B_ACCESS_TOKEN && export E2B_ACCESS_TOKEN
# 观测（可选，需 AgentENV 所在 Linux 主机 + /proc /sys 读权限）
export SNAP_BASE=/var/lib/aenv/persisted-sandboxes/artifacts DISKS=sda,sdb
```

## 2. terminalbench 轨迹（默认自带，可选重导入）

suite 已提交 4 条回放轨迹（delay-model `recorded`）。语料更新或换纯压力形态（`zero`）时，
从 `../agbenchmark/trajectories`（会话级 JSONL：meta/message/result）重导入：

```bash
python -m builder.trajectory_importer import --benchmarks terminalbench --delay-model recorded
```

## 3. terminalbench 子集目录准备（压测前必做）

runner 按 `--trajectory-dir` 整目录执行，而 `artifacts/traces/agbench/` 含全部 57 条轨迹。
**必须新建空目录**（复用旧目录会残留历史轨迹，runner 会读到混入的其他负载）：

```bash
rm -rf artifacts/traces/agbench-terminalbench && mkdir -p artifacts/traces/agbench-terminalbench
cp artifacts/traces/agbench/terminalbench_*.replay.json artifacts/traces/agbench-terminalbench/
python3 - <<'EOF'
import json
from pathlib import Path
d = Path('artifacts/traces/agbench-terminalbench')
full = json.load(open('artifacts/traces/agbench/image-map.json', encoding='utf-8'))
subset = {k: v for k, v in full.items() if k.startswith('terminalbench_')}
actual = {p.name for p in d.glob('*.replay.json')}   # 目录实际轨迹 ID
assert actual == set(subset) and len(actual) == 4, \
    f'dir={sorted(actual)} image-map={sorted(subset)}'   # 三方一致：目录/映射/4 case
json.dump(subset, open(d / 'image-map.json', 'w', encoding='utf-8'), indent=2)
EOF
```

## 4. 镜像构建与模板导入

```bash
python3 builder/image-build/agbench/build_agbench_images.py     # 4 个逐 case 镜像（无 JDK/fixtures 特例）
# aenv/e2b 后端：导入 4 个模板（逐 case tag+push 本地 registry + aenv pull，别名
# agbench-<case_id>；就绪别名自动跳过，失败重试前自动清理 error 状态模板）；docker 跳过
python3 builder/image-build/agbench/import_aenv_templates.py --benchmarks terminalbench
# 回放前核对 4 个模板就绪（runner check 子命令，任一失败即退出）
for t in agbench-terminalbench_01_analyze_access_logs agbench-terminalbench_02_jsonl_aggregator \
         agbench-terminalbench_03_log_summary agbench-terminalbench_04_sql_query_optimization; do
  python -m benchmark.agent_replay.runner check --template "$t" || exit 1
done
```

## 5. 回放压测（三后端）

```bash
# docker 后端
python -m benchmark.agent_replay.runner bench --mode continuous --backend docker --workdir /work \
  --trajectory-dir artifacts/traces/agbench-terminalbench \
  --image-map artifacts/traces/agbench-terminalbench/image-map.json \
  --base-count 4 --ratios 1,2,4 --selection balanced \
  --output-dir benchmark/agent_replay/report/runs/terminalbench-<run_id>

# aenv/e2b 后端：同参数换 --backend aenv（continuous 全后端；pause-resume 仅 aenv）
# 单条冒烟：runner run --mode continuous --backend docker --workdir /work \
#   --template agbench-terminalbench_01_analyze_access_logs:v1 --trajectory artifacts/traces/agbench/terminalbench_01_analyze_access_logs.replay.json
```

要点：`--base-count 4` 对齐子集规模；本池命令普遍短（区别于 TB2 的 490s+ solve 步），
`--command-timeout-sec` 用默认即可；verify 失败先看该 case 末步 stdout。

## 6. 观测与报告

```bash
python -m benchmark.agent_replay.runner export-timings <run 目录>            # 计时导出（runner 子命令）
python3 benchmark/agent_replay/observability/make_obs_report.py <obs_dir> <job_path>... [out.md] [--baseline <dir>]
python -m benchmark.agent_replay.report.step_active_report <run 目录> [--label <label>]
python -m benchmark.agent_replay.report.compare_loop_scheduling <repeat 目录>
```

口径（沙箱耗时 / exec 耗时 / FC 核 / 容器核 / busy-stall 子窗口）见 suite GLOSSARY 与 agent_replay README，判读不得使用其标记避免的口径词。

## 7. 调优 A/B 与性能补丁

- 同口径对照：同轨迹子集、同后端、同 base-count/ratios/repeats，唯一变量是被调优项；轮间
  散布过大时按 suite 噪声口径降级 inconclusive。
- 性能补丁遵循 ADR-0003（声明式 patch.json、独立环境名、与基线口径严格隔离）；suite 的
  tb-perf-patches 家族作用于 TB2 场景变体，跨池使用前须按 ADR-0003 重新注册。
