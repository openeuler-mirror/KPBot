# Agent A/B 用例安全注意事项

> 来自 openeuler/KPBot MR !72 三条安全 review 意见（P1×2、P2×1，已全盘采纳）的教训总结。
>
> 一句话定位：agent A/B 用例把「能读文件、能调工具、可被 prompt 注入的 LLM agent」与「LLM API 凭证、宿主路径、容器特权」放进同一个环境——任何把后一类要素暴露给前一类的用例设计，都构成一条完整攻击链。
>
> 供后续为 KPBot 编写 agent A/B 用例（`Plugins/code-optimizer/skills/<skill>/cases/<case>/`）的工程师参照。

---

## 一、威胁模型：agent A/B 用例为什么特殊

普通性能用例的测试对象是确定性代码；agent A/B 用例的测试对象是 **LLM agent 本身**——它默认具备文件读取与工具调用能力，输出受 prompt 注入与模型自身行为影响，无法静态预判。与此同时，用例运行环境必须持有三样东西：

1. **LLM API 凭证**（如 deepseek API key），供 agent 进程调用模型；
2. **宿主路径**（opencode 二进制、auth.json），通常以 `docker -v` 挂入容器；
3. **容器特权**——若 Dockerfile 无 `USER` 指令、又叠加 `--network host`、docker 默认不 drop capability，容器内进程接近宿主 root 命名空间权限。

这三样任意一样暴露给 agent，攻击链即闭合。MR !72 的三条意见恰好对应三条攻击链。

### 攻击链 1（P1）：凭证外发链

原设计中 run_ab.sh 把宿主 auth.json（LLM provider API key 的存储位置）以只读方式直接挂进容器，容器又以 `--network host` 共享宿主网络命名空间：

```
auth.json 挂入容器（:ro 只防写、不防读）
  → opencode run --auto 启动的 agent 默认具备文件读取 / 工具调用能力
  → agent 读到 /root/.local/share/opencode/auth.json 里的 API key
  → --network host 下容器可向任意外部 endpoint 发起请求
  → API key 外发到任意 URL
  → 宿主 LLM 账户被未授权消费或转售
```

且 SKILL.md 与 prompts 中没有任何禁止 agent 访问凭证的指令——全程无防线。

### 攻击链 2（P1）：恶意代码执行链

LLM 产出的 `opt/kernel_opt.rs` 无签名、无哈希、无 lint，verify.sh 直接 `rustc -O` 编译并运行产物二进制；3 份 Dockerfile 均无 `USER` 指令，进程默认 uid 0：

```
prompt 注入 / 模型被攻陷
  → agent 产出恶意 opt/kernel_opt.rs（无签名 / 哈希 / lint 校验）
  → verify.sh 直接 rustc -O 编译
  → 容器无 USER 指令，进程 uid 0（叠加 --network host、docker 默认未 drop capability）
  → 以容器 root 身份执行任意二进制
  → 容器内任意读写执行 / 资源耗尽 DoS / -O 下构造 UB 影响后续容器
     / 共享 host network 下端口占用、ARP 缓存投毒
```

放大因素：arm-instructions-query SKILL.md 新增的 Rust 对应关系明确鼓励使用 `std::arch::aarch64` 的 unsafe intrinsic（`vld1q_f32`、`__dmb`、`vaddq_f16` 等），本 PR 又把 SVE/f16/FP16 等扩展引入推荐路径——产物代码里出现 unsafe 块是预期内的；若无 lint 与执行隔离，未授权代码的潜在破坏面被进一步放大。

### 攻击链 3（P2）：宿主路径注入链

run_ab.sh 把 OPENCODE_BIN / OPENCODE_AUTH 环境变量直接拼进 `docker -v` 路径，路径检查只警告不退出，DOCKER_RUN_ARGS 允许附加任意 docker run 参数：

```
攻击者在 CI / 共享主机上控制 OPENCODE_BIN / OPENCODE_AUTH / DOCKER_RUN_ARGS 任一环境变量
  → 路径不存在只 echo 警告后继续走到 docker run（默认 BIN 还是 Linux 上几乎必不存在的
     Windows 风格 .exe 路径；默认 AUTH 在 root 执行时解析为 /root/.local/share/opencode/auth.json）
  → docker run 把任意宿主路径（/etc/shadow、SSH key、docker socket、伪装 opencode 的恶意 ELF）挂进容器
  → agent 启动后，宿主关键路径已在容器内暴露
```

---

## 二、逐条注意事项

每条格式：**规则 → 违反后果 → 正确做法 → 代码示例**。示例为真实 bash / Dockerfile 片段，可直接落进 `run_ab.sh` / `Dockerfile` / `entrypoint.sh` / `verify.sh` / `prompts/`。

### 1. 凭证永远不落 agent 可读文件

**规则**：API key 只以 env 形式注入 agent 主进程（opencode 进程），不以文件形式挂入容器；若工具链只认凭证文件，也必须放在 agent 工作目录树（/workspace）之外、600 权限、运行后即删。SKILL.md 与两臂 prompts 中显式写入「禁止读取凭证文件及任何不在 /workspace 下的文件」硬约束（两臂对称，见第 8 条）。

**违反后果**：攻击链 1。`:ro` 挂载只防写不防读，agent 的文件读取工具可以直接 cat 出 API key。

**正确做法**：
- run_ab.sh 不再 `-v` 挂 auth.json，改 `-e` 注入 API key（值来自 CI 凭证存储，不落盘）；
- entrypoint 若必须落文件，用 `mktemp` 到 /tmp、`umask 077`、`trap ... EXIT` 即删，绝不放 /workspace 下；
- SKILL.md / prompts 加禁令兜底。

```bash
# run_ab.sh —— 凭证走 env，不挂文件
cid=$(docker run -d \
    -e SKILL_MODE="${mode}" \
    -e LLM_API_KEY="${LLM_API_KEY:?错误: 未提供 LLM_API_KEY，fail-closed 退出}" \
    "${IMAGE}")
```

```bash
# entrypoint.sh —— 工具链只认凭证文件时的兜底：/tmp + 600 + 用完即删
umask 077
AUTH_FILE="$(mktemp /tmp/opencode_token.XXXXXX)"
printf '%s\n' "${AUTH_JSON_CONTENT}" > "${AUTH_FILE}"   # 内容由 env 传入组装
trap 'rm -f "${AUTH_FILE}"' EXIT
# 之后让 agent CLI（仅主进程）指向 ${AUTH_FILE}，agent 的文件读取工具不可达
```

### 2. 容器不用 --network host

**规则**：docker run 用默认 bridge 网络——访问 LLM provider API 足够；需要更强隔离时做 egress 域名白名单，而不是反向放松。

**违反后果**：攻击链 1 的外发通道 + 攻击链 2 的端口占用 / ARP 缓存投毒。`--network host` 让容器共享宿主网络命名空间，agent 可向任意外部 endpoint 发起请求。

**正确做法**：

```bash
# 默认 bridge：出站经 NAT，与宿主网络命名空间隔离，LLM API 正常可达
cid=$(docker run -d \
    -e SKILL_MODE="${mode}" -e LLM_API_KEY="${LLM_API_KEY:?}" \
    "${IMAGE}")
```

```bash
# 更强隔离（可选）：internal 网络 + 仅经代理放行 LLM provider 域名
docker network create --internal agent-ab-net   # 默认全断外网
# 需要出站的场景：加 sidecar 代理容器，仅放行 api.deepseek.com 等 provider 域名，
# agent 容器 --network agent-ab-net，出站必须经过代理
```

### 3. 判定链文件与 agent 权限隔离：所有权收敛（而非整体 chown）

**规则**：评分链文件（verify.sh / entrypoint.sh / results 判定文件）必须 root 拥有、由 root 侧进程写入；agent（被测方）可写面收敛到产物目录 opt/ 与项目配置目录 .opencode/，agent 进程本身以非特权用户运行。仅 `USER` 非特权 + `chown -R /workspace` 不够：chown -R 把评分脚本与判定文件一并交给 agent，owner 可 chmod 击穿 555 只读位，预置/覆写判定文件或评分脚本即可伪造用例通过。

**违反后果**：攻击链 2。旧模型（chown -R）下 agent 可预置全 true 的 results/result.json——校验器一旦异常退出（实例：超长数字串触发 CPython≥3.10.7 int 转换上限 ValueError 且未被捕获），判定文件保持预置内容，verify 尾部 grep 采信预置，零真实分析通过；亦可直接覆写 verify.sh。若 agent 进程以 uid 0 运行，叠加 docker 默认未 drop capability，破坏面进一步接近宿主 root。

**正确做法**（本仓库两 agent 用例已落地）：

```dockerfile
RUN useradd -m optimizer \
    && mkdir -p /workspace/opt /workspace/results \
    && chown -R optimizer:optimizer /workspace/opt \
    && chmod 755 /workspace /workspace/results
# COPY 的评分链文件保持 root 拥有；entrypoint 以 root 起跑、内部按段降权
USER root
ENTRYPOINT ["/workspace/entrypoint.sh"]
```

entrypoint 按段分权：agent 段 `setpriv --reuid=optimizer --regid=optimizer --init-groups env HOME=... opencode run ...`（输出重定向 FD 由 root shell 先行建立，optimizer 进程写已打开的 FD）；verify 段回 root 运行、判定文件 root 落盘；verify.sh 开头 `rm -f results/result.json` 使任何预置即刻失效。run_ab 白名单已含 `--read-only` / `--tmpfs`，调用方可按需叠加只读根文件系统，把可写面进一步压缩到 tmpfs 挂载点（默认不启用，agent 运行时其余项目内写路径待端到端验证）：

```bash
docker run --read-only \
    --tmpfs /tmp:rw,size=64m \
    --tmpfs /workspace/opt:rw,uid=1000,gid=1000 \
    --tmpfs /workspace/results:rw \
    --tmpfs /workspace/.opencode:rw,uid=1000,gid=1000 \
    ...
```

`--read-only` 下 agent 侧可写面须显式 tmpfs（`/tmp` 供 auth 临时目录与 HOME、`/workspace/.opencode` 供技能装配与 opencode 项目文件），缺则 entrypoint 装配段 fail-closed `exit 1`。另注：项目级权限配置 `opencode.json` 由 entrypoint 运行时写入 /workspace（root 持有，agent 不可改），在只读根上该写无法落盘（真机实测 EROFS）——因此 `--read-only` 组合当前仅适用于不含 agent 段的复验（判定链/装配验证）；完整 agent 运行不得叠加 `--read-only`（deny 配置静默缺失不可接受），待后续把权限配置预置进镜像后再启用该组合。

### 4. LLM 产物代码必须先过 deny 名单 lint 再编译，fail-closed

**规则**：verify.sh 在 `rustc` 之前对 `opt/kernel_opt.rs` 做 deny 名单 grep，命中即写失败原因并 `exit 1`（fail-closed，绝不「警告后继续」）。

**违反后果**：攻击链 2。无校验的 LLM 产物被直接编译并以容器身份执行；本 PR 推荐路径里的 unsafe intrinsic 越多，未授权代码的破坏面越大。

**正确做法**：

```bash
# verify.sh —— 编译前安全 lint（fail-closed）
DENY=(
    'std::process::Command'   # 起子进程 / shell out
    'extern "C"'              # FFI 逃逸到任意本地代码
    'asm!'                    # 内联汇编，可注入任意指令（含特权指令）
    'std::net::'              # 网络外发
    'std::fs::'               # 文件系统读写
)
for p in "${DENY[@]}"; do
    if grep -qnF -- "${p}" opt/kernel_opt.rs; then
        SKILL_MODE="${SKILL_MODE}" DENY="${p}" python3 -c \
          'import json,os;json.dump({"success":False,"reason":"security lint: deny pattern "+os.environ["DENY"],"skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
        echo "[security] deny 名单命中 '${p}'，拒绝编译" >&2
        exit 1
    fi
done
```

**为何该名单对纯计算 kernel 合理**：本类用例的产物只应对内存中的数组做计算，进程 / 网络 / 文件 / FFI / 内联汇编五种能力全部用不到，整体 deny 不影响任何合法产物。NEON/SVE intrinsic（`vld1q_f32`、`vaddq_f16` 等）经 `std::arch::aarch64` 以普通函数形式在 unsafe 块内调用，不经过 `asm!` 宏——deny `asm!` 不会挡住合法 intrinsic 代码，只挡任意指令注入。

**何时调整**：若未来用例确实需要文件 / 网络操作（如 IO 类 benchmark），必须重新评估名单，并为放开的 capability 单独设计隔离（专用 tmpfs、egress 域名白名单等），而不是直接放宽本名单。

### 5. rustc 加安全开关，且两臂编译 flag 必须一致

**规则**：`rustc -O` 追加 `-C overflow-checks=on -C debug-assertions=on`；baseline 与 optimized 两臂使用同一组编译 flag。

**违反后果**：不加开关时，`-O` 下溢出 / 越界等 UB 被静默吞掉，可能影响后续容器；两臂 flag 不一致时，flag 本身成为混杂变量，speedup 差异无法归因于 skill——A/B 对照直接失效。

**正确做法**（A/B 有效性铁律：任何非消融变量都要对称，两臂之间唯一允许的差异是 SKILL_MODE）：

```bash
# verify.sh —— 两臂共用同一组 flag，安全加固对称施加
COMMON_FLAGS=(-O --edition 2021 -C overflow-checks=on -C debug-assertions=on)

rustc "${COMMON_FLAGS[@]}" -o build/baseline   build/base/main.rs 2>build/base_compile.log
rustc "${COMMON_FLAGS[@]}" -o build/optimized  build/opt/main.rs  2>build/opt_compile.log
```

安全措施同理：lint、flag、超时、资源限制等加固必须两臂同等施加，绝不能只加在其中一臂。

### 6. agent 工具面最小化的取舍：禁 bash / webfetch，保留 edit

**规则**：在 opencode 配置层禁用 bash 与 webfetch，保留 edit；文件读取范围限制在 /workspace 内。

**违反后果**：bash 是 shell 逃逸与凭证外发的主通道，webfetch 是把数据（含读到的 key）发往外部 URL 的直达通道；两者对「写一个优化后的 .rs 文件」这一任务都不是刚需。

**正确做法**（以 opencode permission 配置为例，具体语法以所用版本为准）：

```json
{
  "permission": {
    "bash": "deny",
    "webfetch": "deny",
    "edit": "allow"
  }
}
```

**取舍说明**：edit 必须保留——agent 的任务交付物就是 `opt/kernel_opt.rs`，禁掉 edit 用例无法运行。edit 的写范围用两层兜底：prompt 约束「只允许写 opt/」（见第 8 条）+ 文件系统属主收敛（见第 3 条）：/workspace 根与 results/ 归 root 拥有，verify 由 root 侧运行落盘判定文件，agent 可写面收敛到 opt/ 与 .opencode/。run_ab 的 DOCKER_RUN_ARGS 白名单已含 `--read-only` / `--tmpfs`，供调用方按需叠加只读根文件系统加固（默认不启用；启用后 agent 运行时在项目内的其余写路径尚待端到端验证）。若所用 agent CLI 支持写路径白名单，优先用配置层白名单而非仅靠 prompt。

### 7. 环境变量输入 fail-closed + 白名单

**规则**：所有进入 `docker run` 的环境变量路径必须（1）存在性检查 fail-closed；（2）`realpath -e` 解析后落在白名单前缀内；（3）BIN 做文件类型校验；（4）DOCKER_RUN_ARGS 只接受受限参数集合；（5）默认值必须是 Linux 真实路径。

**违反后果**：攻击链 3。能控制任一环境变量的对手可把 /etc/shadow、SSH key、docker socket、伪装 opencode 的恶意 ELF 挂进容器。

**正确做法**：

```bash
#!/usr/bin/env bash
# run_ab.sh —— 环境变量 fail-closed + 白名单
set -uo pipefail

# (5) 默认值是 Linux 真实路径（去掉 .exe 后缀）
BIN="${OPENCODE_BIN:-/usr/local/bin/opencode}"
AUTH="${OPENCODE_AUTH:-$HOME/.local/share/opencode/auth.json}"

# (1) fail-closed：不存在直接退出，而非警告后继续
for path in "${BIN}" "${AUTH}"; do
    [ -e "${path}" ] || { echo "错误: 路径不存在 ${path}" >&2; exit 1; }
done

# (2) realpath 解析 + 白名单前缀（拒绝符号链接绕过与任意路径）
BIN=$(realpath -e "${BIN}")  || exit 1
case "${BIN}" in
    "$HOME"/.local/share/opencode/* \
    |/usr/local/bin/opencode \
    |/usr/local/lib/node_modules/opencode-ai/bin/*) ;;
    *) echo "错误: BIN 不在白名单前缀内: ${BIN}" >&2; exit 1 ;;
esac

# (3) BIN 文件类型校验（防止把任意文件当二进制挂进去执行）
file "${BIN}" | grep -qE 'ELF|PE32' \
    || { echo "错误: BIN 不是 ELF/PE 可执行文件: ${BIN}" >&2; exit 1; }

# (4) DOCKER_RUN_ARGS 只接受受限集合；--privileged/--pid=host/--volume 等一律拒绝
ALLOWED='^(--cpus|--memory|--pids-limit|--rm|--network=bridge)(=|$)'
EXTRA_ARGS=()
for arg in ${DOCKER_RUN_ARGS:-}; do
    if ! printf '%s' "${arg}" | grep -Eq "${ALLOWED}"; then
        echo "错误: DOCKER_RUN_ARGS 不允许的参数: ${arg}" >&2
        exit 1
    fi
    EXTRA_ARGS+=("${arg}")
done
```

### 8. prompts 两臂安全约束对称

**规则**：安全约束写入两臂 prompts 的共同部分（逐字相同）；A/B 消融只允许 SKILL_MODE 一个差异。

**违反后果**：只给 with 臂加安全约束时，「约束是否在场」成为与 SKILL_MODE 混杂的第二变量——若 with 臂因约束变保守导致产出质量或 speedup 变化，A/B 结论无法归因于 skill 本身，整个对照实验作废。

**正确做法**：with-skill.md 与 without-skill.md 携带完全相同的安全约束尾部，例如：

```markdown
安全约束：
- 禁止读取凭证文件及任何不在 /workspace 下的文件。
- 只允许写入 opt/kernel_opt.rs；禁止修改 src/、bench/ 下的任何文件。
- 禁止发起任何网络请求（模型 API 由运行环境代为调用）。
```

SKILL.md 中同样写入「禁止读取凭证文件或任何不在 /workspace 下的文件」硬约束（skill 内容只在 with 臂挂载，属于消融变量本身，不构成混杂）。

---

## 三、快速自查 checklist

编写 agent A/B 用例时逐项打勾：

- [ ] API key 以 env 注入主进程，没有任何 `-v` 挂载凭证文件；如必须落文件，是 /tmp 下 600 权限、运行后删除
- [ ] docker run 无 `--network host`（默认 bridge 即可访问 LLM API）；如需更强隔离已做 egress 域名白名单
- [ ] agent 进程以非特权用户运行；/workspace 根与 results/ 归 root 拥有，verify 以 root 侧运行落盘判定文件，agent 可写面收敛到 opt/ 与 .opencode/；`--read-only` / `--tmpfs` 已入 run_ab 白名单，供调用方按需启用（默认不启用）
- [ ] LLM 产物在编译前过 deny 名单 lint（`std::process::Command` / `extern "C"` / `asm!` / `std::net::` / `std::fs::`），命中即 exit 1
- [ ] deny 名单与用例能力需求匹配：纯计算 kernel 全 deny；需要新 capability 时重新评估隔离，而非直接放宽
- [ ] rustc 带 `-C overflow-checks=on -C debug-assertions=on`
- [ ] 两臂编译 flag、lint、超时、资源限制完全一致（唯一差异是 SKILL_MODE）
- [ ] agent 工具面最小化：bash / webfetch 禁用，edit 保留且写范围有白名单或文件系统兜底
- [ ] SKILL.md 与两臂 prompts 均含「禁止读取凭证及 /workspace 之外文件」硬约束，且两臂约束逐字对称
- [ ] 环境变量路径检查 fail-closed（exit 1 而非警告），realpath 解析 + 白名单前缀，BIN 有文件类型校验
- [ ] DOCKER_RUN_ARGS 只接受受限参数集合，`--privileged` / `--pid=host` / `--volume` 被拒绝
- [ ] BIN / AUTH 默认值是 Linux 真实路径（无 Windows 风格 .exe 后缀）

---

## 四、附：意见出处与本轮修复范围

**出处**：openeuler/KPBot MR !72（agent A/B 测试用例），reviewer mxj1314，2026-10-08 21:04，检视版本 02be337。三条意见：P1×2（凭证外发链、恶意代码执行链）+ P2×1（宿主路径注入链），已全盘采纳。

**MR !72 涉及的 3 个用例**：

| 用例 | 所属 skill |
| --- | --- |
| agent-optimize-token-freq | special-case-optimization |
| agent-optimize-quant-dot | precision-transform |
| agent-optimize-fused-positivity | operation-fusion |

**本轮修复文件范围**：3 用例 ×（run_ab.sh / Dockerfile / entrypoint.sh / verify.sh / case.yaml / prompts×2）+ 3 份 opencode 版 SKILL.md（`opencode/special-case-optimization/`、`opencode/precision-transform/`、`opencode/operation-fusion/` 各一份，加安全约束节；`skills/<skill>/SKILL.md` 为知识内容载体本次不动）。
