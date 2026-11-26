# rocksdb-perf-diagnostics 测试指导

本文给出从"零成本自检"到"KPBot agent 级 A/B"的完整测试流程。所有命令都假设:

```bash
export SKILL_DIR=/path/to/skills/rocksdb-perf-diagnostics
cd "$SKILL_DIR"
```

> 本文只描述测试方法,不修改 KPBot 的 `bin/`、`tests/`、`docs/`。测试脚本都放在本 skill 的
> `evals/` 和 `cases/` 下。

---

## 0. 测试分层

| 层 | 目的 | 入口 | 需要模型 | 产物 |
|---|---|---|---|---|
| 1 | 静态自检:skill 发现、脚本语法、LOG/mock 逻辑 | 直接运行 `scripts/`、`evals/fixtures/` | 否 | 终端输出 |
| 2 | skill 级 A/B:with vs without,验证知识与行为 | `evals/run_ab_subagent.sh` | 是 | `answer.md`、`trace_summary.json`、`grading.json` |
| 3 | agent 级 A/B:容器内 opencode,验证端到端与 skill 调用证据 | `cases/run_all.sh` 或各 case 的 `run_ab.sh` | 是 | `result.json`、`trace.jsonl`、`trace_summary.json` |
| 4 | 真实现场冒烟:真实 LOG / 真实 DB 的只读诊断 | `scripts/parse_rocksdb_log.py`、`scripts/collect_properties.sh` | 可选 | 诊断报告 |

建议顺序:1 → 2 → 3 → 4。第 1 层不过,不要进入后面几层。

---

## 1. 前置条件

### 1.1 通用

- Linux / aarch64(与 KPBot 目标环境一致)
- `bash`、`python3`
- `opencode` 已安装并可执行
- 第 2/3 层需要一个可用的模型与 provider

### 1.2 检查

```bash
opencode --version
opencode models | head -20
opencode debug config | grep -iE 'provider|model' -A8
ls -l ~/.local/share/opencode/auth.json
```

第 3 层额外需要:

```bash
docker info >/dev/null && echo "docker ok"
python3 -c "import yaml; print('pyyaml', yaml.__version__)"   # optional
```

### 1.3 安装形态

- 在 KPBot 仓库里:skill 位于 `Plugins/app-tuner/skills/rocksdb-perf-diagnostics/`
- 通过 `install.sh` 安装到项目:`<project>/.opencode/skills/rocksdb-perf-diagnostics/`

两种形态下 `cases/run_all.sh` 都能工作,因为 `case.yaml` 的 `build.context` 会解析到对应的插件根。

---

## 2. 第 1 层:静态自检

### 2.1 skill 被发现

```bash
opencode debug skill | grep -A1 rocksdb-perf-diagnostics
```

期望:输出 `name: rocksdb-perf-diagnostics` 和 `location: .../SKILL.md`。

### 2.2 语法

```bash
python3 -m py_compile scripts/parse_rocksdb_log.py
for f in $(find . -name '*.sh'); do bash -n "$f" || echo "FAIL $f"; done
```

期望:无输出(全部通过)。

### 2.3 LOG 解析器

```bash
python3 scripts/parse_rocksdb_log.py evals/fixtures/sample_LOG.txt
```

期望关键值:

```
max L0 files seen: 38
stall_slow_l0 3 / stall_slow_mem 1 / stall_slow_pending 1 / stop_l0 2
compaction started=4 finished=4 output=10.00 GiB
flush started=1 finished=1
```

### 2.4 mock 基准

```bash
bash evals/fixtures/mock_db_bench.sh
bash evals/fixtures/mock_db_bench.sh --max_background_jobs=8
```

期望:

```
{"write_buffer_size_mb":64,"max_background_jobs":2,"stall_count":110,"p99_ms":24.00,"throughput_ops":10000}
{"write_buffer_size_mb":64,"max_background_jobs":8,"stall_count":0,"p99_ms":15.00,"throughput_ops":13600}
```

### 2.5 真实 DB 只读采集(可选)

```bash
scripts/collect_properties.sh /path/to/db --full
```

注意:`ldb` 会拿 DB 目录 LOCK。活库请用 checkpoint / secondary,或在应用侧用 `GetProperty`。

**第 1 层通过标准**:2.1–2.4 全部符合期望。

---

## 3. 第 2 层:skill 级 A/B

### 3.1 它测什么

同一任务、同一模型,唯一变量是"有没有这个 skill":

- `with` 工作区:`.opencode/skills/rocksdb-perf-diagnostics`
- `without` 工作区:没有项目 skill
- prompt 来自 `evals/evals.json`;`with` 额外要求读取并遵循 `SKILL.md`,`without` 不提 skill

### 3.2 先 dry-run

```bash
./evals/run_ab_subagent.sh --eval 2 --out /tmp/ab-java --dry-run
sed -n '1,20p' /tmp/ab-java/with/prompt.txt
sed -n '1,20p' /tmp/ab-java/without/prompt.txt
```

检查:

- `with/prompt.txt` 含 "可用 skill: `.opencode/skills/rocksdb-perf-diagnostics`"
- `without/prompt.txt` **不出现** skill 字样
- 两个 prompt 的任务部分完全一致

### 3.3 真跑

```bash
./evals/run_ab_subagent.sh --eval 2 --out /tmp/ab-java --grade --model <provider/model>
```

六个 eval:

| id | 内容 | 关键断言 |
|---|---|---|
| 1 | LOG 诊断 | 识别 Stalling/Stopping、L0、compaction、`Stalls(count)` 口径 |
| 2 | Java(JNI)路径 | Java 无 perf_context;有 EventListener/getThreadList/getMapProperty |
| 3 | 执行循环 | baseline → 只改一个参数 → 复测 → before/after → 保留/回退 |
| 4 | 读路径优化 | `prefix_extractor` + `index_type=kHashSearch`;`memtable_prefix_bloom_size_ratio`;生效范围与验证 |
| 5 | 手段覆盖与收益验证 | 已知手段覆盖表、跳过理由、before/after 收益判据 |
| 6 | 架构前置检查(SVE2 bloom) | 128-bit SVE 不满足 256-bit;跳过;bloom_impl_test 与收益验证 |

### 3.4 产物

```
/tmp/ab-java/with/
  prompt.txt
  trace.jsonl
  opencode.log
  answer.md
  trace_summary.json
  exit_code
  grading.json          # --grade
  grader_prompt.txt     # --grade
/tmp/ab-java/without/...
```

### 3.5 汇总表字段

| 字段 | 含义 | 判读 |
|---|---|---|
| `mode` | `with` / `without` | 唯一变量 |
| `exit` | `opencode run` 退出码 | `0` 成功;`124` 超时;`1` 运行失败;`127` 找不到 opencode |
| `answer_chars` | 抽取到的回答字符数 | 只看"有没有产出";`0` 通常是运行失败或事件格式没抽到 |
| `skills_invoked` | trace 中 `tool=="skill"` 的 `input.name` | `with` 应含 `rocksdb-perf-diagnostics`;`without` 应为空 |
| `pass_rate` | `--grade` 时,通过断言数 / 总断言数 | `1.0` = 全通过;`-` = 未评分或评分失败 |

### 3.6 通过标准

- `with` 的 `skills_invoked` 含 `rocksdb-perf-diagnostics`
- `without` 的 `skills_invoked` 为空(否则 skill 泄漏到 baseline)
- `with` 的 `pass_rate = 1.0`
- `with` 的 `pass_rate > without` 的 `pass_rate`(uplift)
- `answer_chars > 0`(两侧都应产出回答)

### 3.7 判分方式

`pass_rate` 是 **LLM-as-judge**,不是代码断言:

1. 读取该 eval 的 `expectations`(自然语言断言)
2. 把"断言 + `answer.md`"发给评分模型
3. 评分模型返回 `{"results":[{"text","passed","evidence"}],"pass_rate"}`
4. `grading.json` 保存明细

因此:

- 断言要写得可检查、可验证
- 建议跑 2–3 次看稳定性
- 以 `with vs without` 的差值作为 skill 价值,而不是只看绝对值

### 3.8 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `exit=1`,`answer_chars=0` | `opencode run` 失败(模型/provider/网络) | 看 `trace.jsonl` 的 `{"type":"error"}` 和 `opencode.log`;换 `--model` |
| `exit=124` | 超时 | `--timeout 900` |
| `exit=127` | 找不到 `opencode` | 检查 PATH |
| `exit=0` 但 `answer_chars=0` | JSON 事件里没抽到文本 | 把 `trace.jsonl` 前几行发出来,核对事件格式 |
| `without` 也有 `skills_invoked` | 基线工作区仍能发现 skill | 检查是否在 `/home/zyue` 这类已安装 skill 的目录下运行;改用独立 `--out` 工作区 |

---

## 4. 第 3 层:KPBot agent 级 A/B

### 4.1 它测什么

按 `docs/case-spec.md` 的 agent 级契约:

- 容器内运行 opencode
- `with` 把 skill 接入 `.opencode/skills/`,`without` 不接入
- 产出 `result.json`、`trace.jsonl`、`trace_summary.json`
- 判定 correctness / assertions / skill 调用证据

### 4.2 一键运行

```bash
# 列出本 skill 的 case
./cases/run_all.sh --list

# 只打印 build/run 命令
./cases/run_all.sh --dry-run --out /tmp/rocksdb-cases

# 跑全部 agent case
./cases/run_all.sh --out /tmp/rocksdb-cases --model <provider/model> --strict

# 只跑一个
./cases/run_all.sh --case agent-java-path --out /tmp/rocksdb-cases --model <provider/model>

# 镜像已构建,跳过 build
./cases/run_all.sh --no-build --out /tmp/rocksdb-cases --model <provider/model>
```

`run_all.sh` 会:

1. 发现 `cases/*/case.yaml`
2. 按 `build.context` / `build.dockerfile` 构建镜像
3. 调各 case 的 `run_ab.sh --save <out>/<case> <image>`
4. 按 `case.yaml` 的 `assert` 检查 `with.result.json`
5. 输出汇总表;`--strict` 要求 with 成功

> 布局兼容:`run_all.sh` 会检查 `build.context` 下是否存在 `skills/rocksdb-perf-diagnostics`。如果没有(例如在 `some_skills/rocksdb-perf-diagnostics` 或 `.opencode/skills/...` 下运行),会自动在 `$OUT/.build-context` 构造临时 context,再把 Dockerfile 的 `COPY skills/...` 映射过去,不需要手工改 `case.yaml`。

### 4.3 手工运行单个 case(调试用)

```bash
CASE=cases/agent-java-path

# 构建(插件根目录)
CTX=$(cd "$CASE/../../../.." && pwd)
docker build --network host \
  -f "$CTX/skills/rocksdb-perf-diagnostics/$CASE/Dockerfile" \
  -t kpbot/app-tuner-agent-java-path:1.0-openeuler24.03 "$CTX"

# A/B
cd "$CASE"
./run_ab.sh --save /tmp/ab-java-docker
```

执行循环用例:

```bash
cd cases/agent-execute-optimization
./run_ab.sh --save /tmp/ab-exec-docker
```

### 4.4 指定模型与运行时

`run_ab.sh` 会优先使用 CANNBot 环境的 `cannbot` 二进制(存在时),否则退回 `opencode`。凭证整文件 base64 经 `OPENCODE_AUTH_JSON` env 注入,**不挂载 auth.json**;provider 配置通过宿主导出的 `MODEL` / `ANTHROPIC_*` 透传。额外 provider 变量用 `OPENCODE_EXTRA_ENV`(空格分隔):

```bash
MODEL=cannbot/deepseek-v4.1-flash ./run_ab.sh --save /tmp/ab-java-docker

OPENCODE_EXTRA_ENV="MY_PROVIDER_TOKEN MY_PROVIDER_URL" \
  MODEL=cannbot/deepseek-v4.1-flash ./run_ab.sh --save /tmp/ab-java-docker
```

`MODEL` 必须是 `cannbot models` / `opencode models` 里真实存在的 ID(大小写敏感)。`run_all.sh --model` 会把 `MODEL` 导出给 `run_ab.sh`(由 `run_ab.sh` 注入容器)。

entrypoint 会自动检测 CLI 的自动批准参数(`--auto` 或 `--dangerously-skip-permissions`),不需要手工改。

超时:

- `RUN_TIMEOUT`(默认 1200s)是**单臂**超时(WITH / WITHOUT 各一次)
- `run_all.sh --timeout`(默认 2400s)是**单个 case** 的总超时;它会把 `RUN_TIMEOUT` 设为总超时的一半

如果 `REASON` 显示 `model not found`,先跑 `cannbot models` 确认真实 ID;如果显示 `Unexpected server error`,检查容器内的 provider 环境变量/配置是否已透传。

`run_all.sh --model` 已经会自动追加这个 `-e MODEL=...`。

### 4.5 产物与判读

```
<out>/<case>/with.result.json
<out>/<case>/without.result.json
<out>/<case>/with.trace.jsonl
<out>/<case>/with.trace_summary.json
<out>/<case>/without.trace_summary.json
```

Java case 期望:

- `with.result.json`:`success=true`、`assertion_pass_rate=1.0`
- `with.trace_summary.json`:`skills_invoked` 含 `rocksdb-perf-diagnostics`
- `without.result.json`:`success=false`(或低于 with)

执行循环 case 期望:

- `with.result.json`:`success=true`、`one_variable=true`、`changed_param` 非空
- `report_has_before_after`、`report_has_metrics`、`report_has_conclusion` 均为 true
- `without` 通常在这些字段上更弱

### 4.6 测试隔离(重要)

A/B 的 with/without 必须只差"有没有 skill",不能让 agent 读到答案或判分脚本。当前实现:

- **运行时 skill 只包含** `SKILL.md`、`references/`、`scripts/`
- Dockerfile **不 COPY** `evals/`、`cases/`;`verify.sh` / `prompts/` / `entrypoint.sh` 放到 `/opt`,不放在 agent 的 `/workspace`
- entrypoint 把 skill 复制到 `.opencode/skills/` 后,再 `rm -rf .../evals .../cases` 做双保险
- `run_ab_subagent.sh` 只复制运行时目录;fixtures 放到 `$OUT/testdata`,两个臂拿到的都是 testdata 路径,不暴露 skill 目录
- `evals/evals.json`(答案与断言)只由 runner/grader 读取,不进入 agent 工作区

验证:

```bash
find $OUT/with/.opencode/skills/rocksdb-perf-diagnostics -maxdepth 1
# 期望只有 SKILL.md、references、scripts

grep -nE "evals|/path/to/skill" $OUT/with/prompt.txt $OUT/without/prompt.txt
# 期望无输出

docker run --rm --entrypoint bash <image> -lc 'ls -R /staging/skill; ls -R /workspace; ls /opt'
# 期望 /staging/skill 无 evals/cases,/workspace 无 verify.sh
```

### 4.7 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| `docker build` 失败 | 基础镜像/网络/dnf 源 | `--network host`;检查 `openeuler/openeuler:24.03-lts` |
| 容器内 `Unexpected server error` | 模型/provider 不在容器内 | `export MODEL` / `ANTHROPIC_*`;额外变量用 `OPENCODE_EXTRA_ENV`;`DOCKER_RUN_ARGS` 不接受 `-e` |
| `with.result.json` 缺失 | `entrypoint.sh` 失败 | 看 `with.opencode.log`、`with.trace.jsonl` |
| `skills_invoked` 为空 | prompt 没让 agent 调用 skill,或 skill 未安装 | 检查 `entrypoint.sh` 的 `with` 分支和 trace |
| `one_variable=false` | agent 一次改了多个参数 | 重跑;检查 `mock_invocations.log` |

### 4.8 安全约束

- `run_ab.sh` 对 `OPENCODE_BIN` / `OPENCODE_AUTH` **fail-closed**:不存在直接退出;`realpath -e` + 白名单前缀;BIN 做 ELF 魔数校验;可选 `OPENCODE_SHA256` 指纹。
- 默认 `OPENCODE_BIN=/usr/local/bin/opencode`;CANNBot 环境由 `run_all.sh` 自动导出 cannbot 二进制(在白名单内)。
- `DOCKER_RUN_ARGS` 走白名单(仅 `--cpus/--memory/--pids-limit/--rm/--name/--network=bridge/--tmpfs*`);拒绝 `--privileged`、`--pid=host`、`--volume`、`-v`、`--network=host`;agent 运行不支持 `--read-only`(需要可写根写凭证/装配 skill)。
- 凭证整文件 base64 经 `OPENCODE_AUTH_JSON` env 注入,不再 `-v` 挂载;entrypoint 写入非 root agent home(600)后 `unset`。
- agent 以**非 root uid 1000** 运行(`chroot --skip-chdir --userspec=1000:1000 /`),`/workspace/results` 归 uid 1000。
- 权限配置预置在镜像:`/workspace/opencode.json` 和 `/workspace/.opencode/opencode.json`;entrypoint 只做 fail-closed 校验,不运行时写入:
  - `webfetch` / `websearch` / `external_directory` deny
  - `read` 对 `*auth.json`、agent home 下的 auth 路径 deny
  - Java 用例 `bash` 整体 deny
  - 执行用例 `bash` 仅 deny 外发/凭证读取类命令(`curl/wget/ssh/scp/rsync/nc/ncat`、`cat *auth.json`、`cat /proc/*/environ`、`python3 -c`、`env`、`printenv`),mock 仍可运行
- 这些配置对 `--auto` 生效:`--auto` 只放行未被显式 deny 的动作。
- 文档与实现一致性自检:

  ```bash
  grep -nE "OPENCODE_CONFIG_CONTENT|挂载 ~/.config/opencode|默认 --network host|DOCKER_RUN_ARGS 传" cases/TESTING.md cases/*/run_ab.sh cases/*/case.yaml | grep -v "grep -nE" || true
  ```

  期望无输出(或仅“不支持”的说明)。

注意:安全约束会增加 agent 行为约束,可能影响 `one_variable` 等行为指标;失败时先看 `with.opencode.log` 是否 `PermissionDenied`。

---

## 5. 第 4 层:真实现场冒烟

### 5.1 真实 LOG

```bash
python3 scripts/parse_rocksdb_log.py /path/to/db/LOG /path/to/db/LOG.old.* --json
```

对照 `references/log-analysis.md` 检查:

- 是否有 `Stalling writes` / `Stopping writes`
- 停顿原因(L0 / immutable memtable / pending compaction bytes)
- compaction 频率与写放大
- `Stalls(count)` 口径是否正确

### 5.2 真实 DB 只读采集

```bash
scripts/collect_properties.sh /path/to/db --full
```

优先在 checkpoint / secondary 上执行;活库用应用侧 `GetProperty`。

### 5.3 让 agent 产出报告

给 agent 的提示:

> 使用 rocksdb-perf-diagnostics 分析这份 LOG,判断是否有写停顿、compaction 是否影响前台读写,并给出下一步采集项。

检查输出是否包含 `references/report-template.md` 的章节:

1. 基本信息
2. 现象与 SLO
3. 观测数据
4. 瓶颈定位
5. 根因
6. 优化方案
7. 已执行变更与结果
8. 验证与回退
9. 遗留项

并确认每条结论都有证据(LOG 行 / property 值 / 直方图),不是泛泛而谈。

---

## 6. 判分标准汇总

| 层 | 判分 | 通过标准 |
|---|---|---|
| 1 静态 | 固定期望值 | 2.1–2.4 全部符合 |
| 2 skill 级 | LLM-as-judge(`pass_rate`) | with=1.0、with>without、with 调用 skill、without 无调用 |
| 3 agent 级 | `result.json` 确定性断言 | `success=true` + case 特定字段(如 `one_variable=true`) |
| 4 现场 | 人工/报告结构 | 结论有证据、报告章节完整

---

## 7. 测试矩阵

| 用例 | 层级 | 入口 | 关键断言 | 参考结果 |
|---|---|---|---|---|
| eval 1 LOG 诊断 | 2 | `run_ab_subagent.sh --eval 1` | Stalling/Stopping、L0、compaction、`Stalls(count)` | with 8/8 vs without 8/8(不区分) |
| eval 2 Java 路径 | 2 | `run_ab_subagent.sh --eval 2` | perf_context 不可用、EventListener、getThreadList、getMapProperty | with 8/8 vs without 5/8 |
| eval 3 执行循环 | 2 | `run_ab_subagent.sh --eval 3` | baseline、单变量、before/after、保留/回退 | with 6/6 vs without 5/6 |
| eval 4 读路径优化 | 2 | `run_ab_subagent.sh --eval 4` | prefix hash index、memtable prefix bloom、生效范围、验证指标 | 需可用模型 |
| eval 5 手段覆盖与收益验证 | 2 | `run_ab_subagent.sh --eval 5` | 覆盖表、跳过理由、before/after 收益判据 | 需可用模型 |
| eval 6 架构前置检查 | 2 | `run_ab_subagent.sh --eval 6` | 环境检查、SVE 向量长度、跳过结论、正确性/收益验证 | 需可用模型 |
| agent-java-path | 3 | `run_all.sh --case agent-java-path` | success、assertion_pass_rate、skills_invoked | 需可用模型 |
| agent-execute-optimization | 3 | `run_all.sh --case agent-execute-optimization` | success、one_variable、changed_param | 需可用模型 |

> 参考结果是之前用 subagent 人工判分得到的;`run_ab_subagent.sh --grade` 的 LLM 评分可能略有出入,以 `grading.json` 的 evidence 为准。

---

## 8. CI 建议

```bash
# 1) 静态自检
python3 scripts/parse_rocksdb_log.py evals/fixtures/sample_LOG.txt
bash evals/fixtures/mock_db_bench.sh

# 2) skill 级(固定模型)
./evals/run_ab_subagent.sh --eval 2 --grade --model "$MODEL" --out "$OUT/skill"

# 3) agent 级(镜像可预构建)
./cases/run_all.sh --no-build --model "$MODEL" --out "$OUT/agent" --strict
```

- 镜像在 CI 里预构建并缓存
- 固定模型版本,避免评分漂移
- 保留 `answer.md` / `grading.json` / `result.json` / `trace.jsonl` 作为证据
- 失败时优先看 `opencode.log` 与 `trace.jsonl` 的 `error` 事件
