# 结果目录与验证

## 目录结构

```text
<output-root>/<YYYYMMDD_HHMMSS>_<pid>/
├── approved-plan.json          # 用户确认的最终计划
├── metadata.json               # host/kernel/commit/template/plan+approval hash；不写 API Key
├── preflight.json              # 正式测试前的完整检查
├── prepare.json                # in_progress/completed/failed + 每步 action journal
├── prepare-backups/            # 被修改启动脚本的备份（如有）
├── smoke.log                   # 真实创建、run_code、删除
├── template-create/            # cube-bench 原始 JSON + 每档日志
├── density/                    # create-only JSON + 每批日志
├── density.csv                 # 累计实例、MemAvailable、均摊变化
├── snapshot/                   # 官方表格输出日志
├── dirty_page/
├── snapshot_create/
├── rollback/
├── clone/
├── pause_resume/
├── create-verdict.json         # 复用 bench-protocol 的 create 分布/轮次判读
├── results.json                # 机器可读逐档状态、指标、清理结果
└── report.md                   # 汇总入口
```

独立 `check --json` 只向 stdout 输出预检 JSON；可选 `--output /path/preflight.json` 同时原子落盘。
输出父目录必须可信，最终符号链接会被拒绝；0600 唯一临时文件独占创建并通过 `os.replace` 替换目录项。
`cubesandbox-bench-preflight` 默认写当前目录 `preflight.json`，即使检查 FAIL（包括缺少 cubemastercli）也
写出本次 `ok=false` 证据，不保留同路径旧通过结果。
独立 `prepare` 的授权/参数失败在创建目录前退出，没有 `prepare.json`，看退出码与 stderr；进入状态机后写入
`prepare_<时间戳>_<pid>`，准备动作失败保留 `status=failed` 和 action journal。八个单场景入口使用同一结果目录和成功判据，
并在 `metadata.sections` 中只保留各自固定章节；dry-run 不生成正式结果目录。

## 成功判定

只有同时满足以下条件才可称本次执行完整通过：

1. prepare（如选择）和正式预检全部 PASS；
2. 真实冒烟包含 `SMOKE_OK`、退出码为 0 且 Guest result.error 为空；
3. 每个所选档位退出码为 0；create JSON 通过 config/summary/create/raw 完整性校验，
   errors=0、success_rate=1，warmup 无失败；
4. 差集清理成功并等待 API 沙箱精确回到各 job baseline；density 在最后一批后立即清理并回空，
   再进入后续章节，清理请求失败也会保留为失败证据；
5. create 分析器退出码为 0 且生成 verdict（选择该章节时）；
6. `results.json.ok=true`。

任一失败会停止后续测试并生成非零退出。部分表格存在不代表整个章节成功；分析器退出码 3
表示报告不可读或 schema 不完整，会使总体失败，不能仅因有 verdict 文件就判为通过。

## 清理边界

普通档位在命令前后记录 API 沙箱 ID，仅删除差集；中断/异常也会先写失败记录并进入清理。
density 只有在完整 baseline 成功读取后才“武装”最终清理，并在整章结束后统一删除相对空机
基线新增的实例。测试窗口有其他创建者时，不仅数据无效，差集语义也可能包含对方实例，因此
必须独占并使用宿主机锁。

Snapshot、模板以及非 API 可见的残留不在沙箱差集内。异常时读取日志中的 snap/tpl ID，向用户
报告并请求授权后另行清理；不得在报告里声称这些资源已自动清空。

## 自验顺序

```bash
# 1. 静态与 Tier-A 契约
bash cases/bench-contract/run.sh

# 2. 计划展开（无环境访问）
python3 scripts/cubesandbox_bench.py run --dry-run --repo /mock/CubeSandbox

# 3. 目标机只读预检
python3 scripts/cubesandbox_bench.py check --repo /path/CubeSandbox --template-id tpl-xxx

# 4. 用户确认后才允许正式 run / run --prepare
```

仓库已有修改时不要用 dry-run 的通过替代真实环境验证，也不要为测试 skill 而重置用户源码。
