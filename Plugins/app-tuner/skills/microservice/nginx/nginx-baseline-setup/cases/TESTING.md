# nginx-baseline-setup 用例运行指南

本目录是 `nginx-baseline-setup` skill 的用例集（自 `nginx-optimization` 拆分而来，原多用例结构按单一职责拆分到各 skill）。

当前用例：

- `agent-tuning-loop` — tier agent：mock 压测工具（确定性指标、零副作用）验证 agent 是否严格遵守调优循环纪律（先基线、单变量、before/after 对比、保留/回退结论）。**A/B 对照**：with（接入 skill）vs without。

## 运行方法

在 KPBot 插件根目录（包含 `skills/` 的目录）：

```bash
# 一键运行（发现 cases/*/case.yaml → build → run_ab → 断言评估）
bash skills/microservice/nginx/nginx-baseline-setup/cases/run_all.sh

# 常用选项
bash skills/microservice/nginx/nginx-baseline-setup/cases/run_all.sh --list
bash skills/microservice/nginx/nginx-baseline-setup/cases/run_all.sh --case agent-tuning-loop
bash skills/microservice/nginx/nginx-baseline-setup/cases/run_all.sh --strict
bash skills/microservice/nginx/nginx-baseline-setup/cases/run_all.sh --dry-run
```

单独运行 A/B（调试）：

```bash
bash skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/run_ab.sh --save results-ab
```

镜像 tag 与本 skill 绑定，避免与 `nginx-optimization` 同名用例互相覆盖：

```bash
docker build \
  -f skills/microservice/nginx/nginx-baseline-setup/cases/agent-tuning-loop/Dockerfile \
  -t kpbot/app-tuner-agent-nginx-baseline-tuning-loop:1.0-openeuler24.03 .
```

## 环境要求

- docker
- opencode 二进制（`OPENCODE_BIN`，只读挂载，宿主路径经白名单 + ELF 校验）
- opencode auth.json（`OPENCODE_AUTH`，整文件 base64 经 `OPENCODE_AUTH_JSON` env 注入，不做卷挂载）
- 容器可访问模型 API（默认 bridge；确需 host 时显式 `DOCKER_RUN_ARGS=--network=host`）
- python3 + PyYAML 为 `run_all.sh` 硬依赖（缺失时明确报错退出）

## 断言与证据门禁

`run_all.sh` 按 `case.yaml` 的 `assert` 求值：

- `exit_code`：with 臂容器退出码
- `json_field`（file 固定为 with 臂 `results/result.json`）：
  - `success` / `one_variable`：verify 检查结论
  - `skills_invoked` + `op: contains`：**硬门禁**——with 臂必须真实调用
    `nginx-baseline-setup`；未调用时该 case 判 `invalid` 而非 `pass`

`run_ab.sh` 自身也做本地硬门禁：with 臂 trace 中没有 `skills_invoked=nginx-baseline-setup`、
without 臂反而加载了 skill、任一臂 `result.json` 缺失或为空，都会以非 0 退出，不产生 uplift 证据。

`ab.uplift` 与 `run_ab.sh` 的 `summary.uplift` 均为**差值**（`pass_rate_with - pass_rate_without`，
单位百分点），避免比值/差值两套语义混用。

## 安全边界

- 凭证不落卷挂载；entrypoint 解码到 runner HOME 下 600 文件，agent 启动前 unset，退出时删除。
- agent 以非特权用户 `runner` 运行；`verify.sh` / `entrypoint.sh` / prompts / fixtures 由 root
  拥有且不可写，构建期 `.pristine.sha256` 在 agent 前后校验。
- `results/` 为 root 700（trace/result 判定产物），agent 只写 `work/`（runner 700，
  report/base.conf/mock 日志）；`verify.sh` 以 root 运行并先 `rm -f results/result.json`，
  agent 无法预置/替换判定结果。
- 两臂 prompts 带相同的安全约束（不读取 `/workspace` 之外文件/凭证、不做网络请求）。

## 产物

- `<out>/agent-tuning-loop/summary.json`：双臂 exit/success/pass_rate/uplift/evidence_complete
- `with.json` / `without.json`：各臂 result.json（含 skills_invoked）
- `with.report.md` / `without.report.md`：agent 产出
- `with.trace.jsonl` / `with.trace.json`：工具与 skill 调用证据
