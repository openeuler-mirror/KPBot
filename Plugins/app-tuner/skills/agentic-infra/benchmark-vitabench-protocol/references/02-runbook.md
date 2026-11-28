# VitaBench 回放压测 Runbook

> 命令契约以 suite（commit `673be52`）`benchmark/agent_replay/README.md` 与
> `builder/trajectory_importer/README.md` 为真源；本文只固化 vitabench 路径与差异点。

## 1. 环境准备

前提：真源为个人仓，需先获得仓库访问授权（未授权账号 403），公开托管迁移跟踪见 issue #23。

```bash
git clone https://gitcode.com/linhandy/agent-workload-benchmark-suite.git && cd agent-workload-benchmark-suite
git checkout 673be52                      # 基准 commit（SKILL.md 真源声明）
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

## 2. vitabench 轨迹（默认自带，可选重导入）

suite 已提交 4 条回放轨迹（delay-model `recorded`，保留录制思考时间）。语料更新或换
纯压力形态时，从 `../agbenchmark/trajectories`（会话级 JSONL：meta/message/result）重导入：

```bash
python -m builder.trajectory_importer import --benchmarks vitabench --delay-model recorded
# 纯压力形态（零延迟）：上条改 --delay-model zero --fixed-sec 0
```

延迟模式核对：重导入后检查 `artifacts/traces/agbench/import-manifest.json` 的
`delay_model` 字段（`recorded`/`zero`），与期望形态一致后再进入 §3。

## 3. vitabench 子集目录准备（压测前必做）

runner 按 `--trajectory-dir` 整目录执行，而 `artifacts/traces/agbench/` 含全部 57 条轨迹。
**每轮创建独立空目录**（不删旧目录，避免误删手工保留的轨迹或其他回放仍在读取的映射）：

```bash
SUBSET_DIR=$(mktemp -d artifacts/traces/agbench-vitabench.XXXXXX)   # 本轮独立空目录
cp artifacts/traces/agbench/vitabench_*.replay.json "$SUBSET_DIR"/
python3 - "$SUBSET_DIR" <<'EOF'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
full = json.load(open('artifacts/traces/agbench/image-map.json', encoding='utf-8'))
subset = {k: v for k, v in full.items() if k.startswith('vitabench_')}
actual = {p.name for p in d.glob('*.replay.json')}       # 目录实际轨迹
expected = {"vitabench_01_ota_hotel_booking.replay.json", "vitabench_02_delivery_rice_noodles.replay.json", "vitabench_03_instore_gym_search.replay.json", "vitabench_04_redis_session_store.replay.json"}   # 画像 case 清单
assert actual == set(subset) == expected, \                # 三方一致：目录/映射/画像清单
    f'dir={sorted(actual)} map={len(subset)} expected={len(expected)}'
json.dump(subset, open(d / 'image-map.json', 'w', encoding='utf-8'), indent=2)
EOF
```

## 4. 镜像构建与模板导入

```bash
python3 builder/image-build/agbench/build_agbench_images.py   # 4 个逐 case 镜像（04 含 Redis）
# aenv/e2b 后端：导入 4 个模板（逐 case tag+push 本地 registry + aenv pull，别名
# agbench-<case_id>；就绪别名自动跳过，失败重试前自动清理 error 状态模板）；docker 跳过
python3 builder/image-build/agbench/import_aenv_templates.py --benchmarks vitabench
# 回放前核对 4 个模板就绪（runner check 子命令，任一失败即退出）
for t in agbench-vitabench_01_ota_hotel_booking \
         agbench-vitabench_02_delivery_rice_noodles \
         agbench-vitabench_03_instore_gym_search \
         agbench-vitabench_04_redis_session_store; do
  python -m benchmark.agent_replay.runner check --template "$t" || exit 1
done
```

## 5. 回放压测（三后端命令矩阵）

```bash
# docker 后端
python -m benchmark.agent_replay.runner bench --mode continuous --backend docker --workdir /work \
  --trajectory-dir "$SUBSET_DIR" --image-map "$SUBSET_DIR/image-map.json" \
  --base-count 4 --ratios 1,2,4 --selection balanced \
  --output-dir benchmark/agent_replay/report/runs/vitabench-<run_id>

# AgentENV（aenv）后端——continuous 模式
python -m benchmark.agent_replay.runner bench --mode continuous --backend aenv \
  --base-count 4 --ratios 1,2,4 --selection balanced \
  --trajectory-dir "$SUBSET_DIR" --image-map "$SUBSET_DIR/image-map.json" \
  --output-dir benchmark/agent_replay/report/runs/vitabench-<run_id>

# AgentENV（aenv）后端——pause-resume 模式（仅此后端支持）
python -m benchmark.agent_replay.runner bench --mode pause-resume --backend aenv \
  --base-count 4 --ratios 1 \
  --trajectory-dir "$SUBSET_DIR" --image-map "$SUBSET_DIR/image-map.json" \
  --output-dir benchmark/agent_replay/report/runs/vitabench-<run_id>

# E2B 自托管栈后端——与 aenv 是两套沙箱服务（均由 E2B SDK 驱动；e2b 数据面走
# <port>-<id>.<domain> 子域路由，模板已按 §4 导入）；凭证用 §1 的 E2B_* 变量
python -m benchmark.agent_replay.runner bench --mode continuous --backend e2b \
  --e2b-domain <your-subdomain>.e2b.app \
  --base-count 4 --ratios 1,2,4 --selection balanced \
  --trajectory-dir "$SUBSET_DIR" --image-map "$SUBSET_DIR/image-map.json" \
  --output-dir benchmark/agent_replay/report/runs/vitabench-<run_id>

# 单条冒烟（docker）：runner run --mode continuous --backend docker --workdir /work \
#   --template agbench-vitabench_01_ota_hotel_booking:v1 --trajectory artifacts/traces/agbench/vitabench_01_ota_hotel_booking.replay.json
```

要点：`--base-count 4` 对齐子集规模；verify 失败先看该 case 末步 stdout。

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
- 性能补丁遵循 ADR-0003（声明式 patch.json、独立环境名、与基线口径严格隔离）；跨池使用
  其他 benchmark 的补丁家族前须按 ADR-0003 重新注册。
