# UADK / HPRE 密码卸载 Playbook

鲲鹏 HPRE 密码加速卡通过 UADK 把 TLS 握手 RSA 运算卸载到硬件的判定、使能、调优与验证。核心操作全部由本目录 `scripts/` 承载，本文只保留判定依据与约束。

## 1. 适用范围

| 适用 | 不适用 |
|---|---|
| HPRE / KAE 密码加速卡（`lspci` 类名 `Network and computing encryption device [1000]`，PF `19e5:a258` / VF `19e5:a259`） | 对称加密体数据、摘要运算、ECDSA/Ed25519 证书 |
| RSA 签名 / RSA 解密，即 TLS 握手 | AES 等对称加密体数据、ECDSA/Ed25519 证书 |
| 裸机，或 KVM/VMware + HPRE VF 透传（`uacce_mode=2`） | 未挂载 `/dev/hisi_hpre-*` 的容器 |
| OpenSSL 1.1.1 + `engines-1.1` + UADK | OpenSSL 3.x（引擎 ABI 不兼容） |
| 握手是瓶颈：短连接、RSA 证书、会话复用已关闭 | 长连接或已开 session cache/tickets |

## 2. 判定证据

用 `scripts/collect_hpre_evidence.sh` 一次性采集，四个条件同时成立才进入使能：

| 证据 | 期望 | 不满足的结论 |
|---|---|---|
| `/sys/class/uacce/hisi_hpre-*` + `/dev/hisi_hpre-*` | 存在 | 设备未注册，先修 `uacce_mode` |
| `uacce_mode` | `2` | `0` 无 uacce 设备；`1` 在 VM 中失败 |
| `openssl engine -t uadk_engine` / `-t e_sse` | `[ available ]` | 引擎/依赖库/环境变量问题 |
| 证书 `Public Key Algorithm` | `rsaEncryption` | 卸载不生效，不是故障 |

`hisi_hpre-N` / `/dev/hisi_hpre-N` 的 `N` 是全系统 UACCE 设备注册顺序号（`uacce_xa` 全局分配），不保证从 0 开始；SEC/ZIP 与 HPRE 同时启用时 HPRE 可能是 `-1` 或更高，判定必须用 `hisi_hpre-*` 通配。

补充判据：worker `/proc/<pid>/maps` 中无 `e_sse.so`/`uadk_engine.so`/`libasf.so` 说明未加载；`QM_DFX_SEND_SQE_TO_ACC_CNT` 不随握手增长说明未真正卸载。`hotspot_function_rank` 中 RSA/握手符号占比高且握手率随并发上升，才说明有收益空间。

判定以 `/sys/class/uacce` 与「驱动绑定到 `hisi_hpre`」为准；`lspci` 类名在不同平台可能是 `System peripheral` 或 `Network and computing encryption device`，不要用类名做唯一判据。SQE 计数器的值在 `regs` 里是十六进制（如 `0x0053d5ea`），比对时先转十进制，且 `debugfs` 必须已挂载。

## 3. 数据流

```text
客户端 --TLS 握手--> nginx worker -> OpenSSL 1.1.1
   -> e_sse (ASF 调度，按 quota 限速) -> libasf.so -> uadk_engine.so
   -> /dev/hisi_hpre-N (UACCE) -> 宿主机 HPRE 硬件
```

`uadk_engine` 可单独 `ssl_engine uadk_engine;` 直连，用于验证硬件通路；`e_sse` 是带 ASF 调度与 quota 的正式路径。

## 4. 组件

| 组件 | 来源 | 位置 |
|---|---|---|
| OpenSSL 1.1.1 | openssl.org 源码编译 | `/usr/local/openssl` |
| UADK `libwd` / `libwd_crypto` | Linaro/uadk 源码，或 `dnf install libwd`（部分源为 `kae-driver`） | `/usr/local/lib` 或 `/usr/lib64` |
| `uadk_engine.so` | `dnf install uadk_engine`，或 Linaro/uadk_engine 源码 | `/usr/lib64/engines-1.1/` |
| `libasf.so` + `e_sse.so` | ASF 发布包（非开源仓库，`dnf provides` 查不到）；现场常已在 `/usr/local/nginx/lib/` | `/usr/lib64/`、`/usr/lib64/engines-1.1/`，装完必须 `ldconfig` |
| `libcjson.so.1` / `libnuma.so.1` | `dnf install cjson numactl-libs` | `/usr/lib64/` |

三者必须同一套版本；`uadk_engine.so` 的 engine id 是 `uadk_engine`，与 KAE 的 `kae.so` 不能混用。

## 5. 核心三步

```bash
# 1. 采集（只读）；--nginx-conf 与后续使能/验证使用同一个实际生效路径
bash scripts/collect_hpre_evidence.sh --pid <nginx-worker-pid> \
     --nginx-conf /usr/local/nginx/conf/nginx.conf --output /tmp/hpre-evidence.json

# 1b. 采集并接入本轮证据快照（推荐；四项绑定参数必填）
bash scripts/collect_hpre_evidence.sh \
     --evidence-dir <evidence_snapshot_dir> \
     --current-run-id <current_run_id> \
     --current-run-started-at <current_run_started_at> \
     --target-identity <target_identity.json> \
     --nginx-conf /usr/local/nginx/conf/nginx.conf \
     --pid <nginx-worker-pid>

# 2. 使能（默认 dry-run；--execute 必须带批准 ID）
bash scripts/enable_uadk_offload.sh --nginx-conf /usr/local/nginx/conf/nginx.conf
bash scripts/enable_uadk_offload.sh --execute --approved-change-id <change-id> \
     --nginx-conf /usr/local/nginx/conf/nginx.conf \
     --nginx-bin /path/to/nginx --quota 450OPS --throughput 2000OPS

# 3. 验证（HTTPS 成功握手数 + SQE 增量 + 进程 maps）；--url 必须为 https
bash scripts/verify_hpre_offload.sh --url https://<ip>:10001/index.html --requests 50 --concurrency 20
bash scripts/verify_hpre_offload.sh --url https://<ip>:10001/index.html --pid <server-pid> --requests 50   # 非 nginx 服务端
bash scripts/verify_hpre_offload.sh --url https://<vm-ip>:10001/index.html --ssh root@<VM_IP> --requests 50 # 宿主机验证 VM 内服务
```

`verify_hpre_offload.sh` 会校验 `--requests`/`--concurrency`/`--pid` 为纯数字、`--url` 必须为 https 且不含引号与 shell 元字符；总请求 ≤10000、在途并发 ≤200，分批等待并统计成功握手数与 HTTP 状态。只有成功握手数 ≥90% 请求、SQE 增量覆盖成功握手数且不超过 3 倍容差时才 PASS；共享 PF 计数明显无法归因时返回 `INSUFFICIENT`。远端 `ssh` 默认 `StrictHostKeyChecking=accept-new`，需要固定指纹时用 `SSH_OPTS='-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/path/known_hosts'`。

使能脚本做四件事：`uacce_mode=2` 就位 → 安装/校验依赖与引擎 → 写 OpenSSL 引擎 cnf 与 `/etc/asf_conf/{asf_conf,scheduler,dev_metadata/uadk_engine}.json` → 在 nginx 配置顶层保留/插入唯一的 `ssl_engine e_sse;` 并自检两个引擎。

覆盖写前先备份：nginx 配置、已有 `$SSL_CNF` 与 `/etc/asf_conf/*.json` 都生成 `.bak.<ts>` 并打印回滚命令；新建的 cnf 会明确标注“rollback: rm -f”。改写后用 `--nginx-bin`（不给则按 `$NGX_PREFIX/sbin/nginx` → `$PATH` 探测）跑 `nginx -t`：通过才继续；失败自动用 `.bak` 回滚 nginx.conf 与本次改写的 cnf 并 exit 1；缺运行库或找不到 nginx 二进制时同样回滚并 exit 3，绝不以 exit 0 留下未校验配置。

nginx 侧最小改动（`ssl_engine` 是 nginx **main 上下文**指令，全文件只能有一条，放在 `http {}` 外；放进 `http {}`/`server {}` 会被 `nginx -t` 拒绝）：

```nginx
ssl_engine e_sse;

http {
    ssl_session_cache off;
    ssl_session_tickets off;
    ssl_prefer_server_ciphers on;
}
```

## 6. 硬约束

- `uacce_mode` 必须为 `2`；虚拟机必须透传 HPRE VF，`uacce_mode=1` 需要 SVA，VM 中会失败。
- nginx 必须链接 OpenSSL 1.1（`libssl.so.1.1`）；引擎目录必须是 `engines-1.1`。
- 同一主机常存在多个 nginx：用 `ldd <nginx> | grep libssl` 选链接 `libssl.so.1.1` 的那个；链接 `libssl.so.3` 的无法加载 `engines-1.1`。启动时如报 `libssl.so.1.1 => not found`，加 `LD_LIBRARY_PATH=/usr/local/openssl/lib`。
- 证书必须 RSA；ECDSA 证书不产生卸载计数。
- 短连接场景 `ssl_session_cache` / `ssl_session_tickets` 必须 `off`，否则握手被复用吞掉。
- `quota` 不得超过 `dev_metadata` 中同算法的 `throughput`；`enable_uadk_offload.sh` 写盘前按 SN/processor/device 逐项校验（同单位比较；`--only-quota --all-quotas` 也必须逐项匹配，任一上限缺失、单位不可比或超限即 exit 2）。本版本 `sched_mode` 仅 `priority`。
- 驱动、`modprobe`、SR-IOV/VF 透传属于高风险动作，只在主流程 `approved_execute` 后由执行验证 subagent 落地。

## 7. 调优

| 参数 | 位置 | 方法 | 风险 |
|---|---|---|---|
| `quota` | `scheduler.json` | 从小值逐档提高至吞吐不再增长，≤ throughput | 低，重启 nginx 生效 |
| `throughput` | `dev_metadata/uadk_engine.json` | 与 quota 同单位且 ≥ quota | 低 |
| `worker_processes` | nginx.conf | `1×/2×/4× vCPU` 扫描拐点，吞吐下降即回退 | 中 |
| `worker_cpu_affinity` | nginx.conf | 与 worker 数一致，每核重复相应次数 | 中，与 cpu-affinity 协同 |
| `ssl_session_cache/tickets` | nginx.conf | 短连接场景 `off` | 低，但提高握手量与 CPU |

单变量逐轮：改一个参数 → 重启 nginx → 压测 3 轮取均值 → 保留"吞吐更高 + SQE 持续增长 + CPU 合理"的档位。多虚拟机共享同一 PF 时，各 VM 的 `quota` 之和不得超过 PF 实际能力。

## 8. 验收

- `curl -sk https://<ip>:10001/index.html` 返回 200。
- `QM_DFX_SEND_SQE_TO_ACC_CNT` 增量覆盖成功握手数（不得低于成功握手数，也不得超过其 3 倍；超出视为共享 PF 噪声、证据不足）。
- worker `/proc/<pid>/maps` 含 `e_sse.so`、`uadk_engine.so`、`libasf.so`。
- A/B（仅增删 `ssl_engine e_sse;`）吞吐高于软件基线且 P95 不劣化。

任一不满足不得标记 `accepted`。

## 9. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `/sys/class/uacce` 为空 | `uacce_mode=0` | `modprobe -r hisi_hpre; modprobe hisi_hpre uacce_mode=2` |
| `iommu not support sva` | `uacce_mode=1` 在 VM 中 | 改 `uacce_mode=2` |
| `libssl.so.1.1 => not found` | 未设库路径 | `export LD_LIBRARY_PATH=/usr/local/openssl/lib` |
| `engines-3/e_sse.so: cannot open` 或 version incompatibility | 用 OpenSSL 3 加载 1.1 引擎 | 统一 OpenSSL 1.1.1 |
| 引擎 unavailable | `OPENSSL_CONF`/`OPENSSL_ENGINES` 未设或依赖缺失 | 设环境变量，查 `ldd` not found |
| `ldd e_sse.so` 显示 `libasf.so => not found` 但文件在 `/usr/local/nginx/lib` | libasf 不在加载路径 | `install -m 0755 /usr/local/nginx/lib/libasf.so /usr/lib64/ && ldconfig` |
| SQE 计数读不到 | `debugfs` 未挂载或无权限 | `mount -t debugfs none /sys/kernel/debug`；计数器为十六进制 |
| 引擎已加载但计数不增长 | ECDSA 证书 / 会话复用 / 未加 `ssl_engine` | 换 RSA 证书、关 session cache、补指令 |
| 压测无提升 | quota 过低 / `enabled=false` / 瓶颈不在握手 | 提 quota、查 dev_metadata、确认短连接 |
| `modprobe -r` 失败 | nginx 仍占用设备 | 先停 nginx |

## 10. 回滚

多实例主机禁止 `pkill nginx`（会连坐其他实例）。按本次变更记录的目标服务单元或 master PID 操作：

```bash
# 方式一：本次变更记录的系统服务单元（推荐）
NGINX_UNIT=<本次变更记录的服务单元，如 nginx-blue.service>
systemctl stop "$NGINX_UNIT"
cp -p <nginx.conf.bak.ts> <nginx.conf>
cp -p <ssl.cnf.bak.ts> <ssl.cnf>          # 本次新建的 cnf 则 rm -f
nginx -t -c <nginx.conf> && systemctl start "$NGINX_UNIT"
systemctl is-active "$NGINX_UNIT"

# 方式二：非 systemd 实例，使用该 master 的 pid 文件
NGINX_PID_FILE=<本次实例 pid 文件，如 /run/nginx-blue.pid>
kill -QUIT "$(cat "$NGINX_PID_FILE")"
cp -p <nginx.conf.bak.ts> <nginx.conf>
nginx -t -c <nginx.conf> && nginx -c <nginx.conf>
```

`enable_uadk_offload.sh` 的每个写操作都有对应反向动作：已有 cnf 用 `.bak.<ts>` 恢复、本次新建的 cnf 删除、用 `.bak.<ts>` 恢复被覆盖的 `/etc/asf_conf/*.json`、恢复 nginx.conf 备份并 `nginx -t`、移除引擎文件后 `ldconfig`、`--quota <原值>` 复原。主流程 `record-execution` 的 `reverse_cmd` 直接取这些命令，并记录目标服务单元/PID 与恢复后健康检查结果。

## 11. 动作映射

| action_id | title | category | priority | change_mode | requires_root | risk |
|---|---|---|---|---|---|---|
| `accel-uadk-000` | 确认本节点无 HPRE，跳过卸载 | evidence | low | `analysis_only` | false | low |
| `accel-uadk-001` | 采集 HPRE 卸载证据 | evidence | high | `analysis_only` | false | low |
| `accel-uadk-002` | uacce_mode / VF 就位 | driver | high | `system_reboot` | true | high |
| `accel-uadk-003` | 安装依赖与引擎 | library | high | `online` | true | medium |
| `accel-uadk-004` | 写引擎与 ASF 配置 | config | high | `restart_required` | true | medium |
| `accel-uadk-005` | quota / worker 调优 | tuning | medium | `restart_required` | true | low |
| `accel-uadk-006` | 卸载验证 | verification | high | `analysis_only` | true | low |

每个动作必须写全 SKILL.md 输出契约中的 14 个字段，并额外给出 `commands_dry_run`/`commands_execute`。`rejection_criteria` 至少覆盖：证书非 RSA、会话复用无法关闭、OpenSSL 主版本冲突、SQE 不增长、A/B 无提升、quota 已达 throughput。`expected_effect`/`expected_gain_metric` 只能写“待本轮 A/B 验证”，不得引用历史数字。`rollback` 必须可独立执行，并包含用 `.bak.<ts>` 备份恢复 nginx.conf（`enable_uadk_offload.sh` 会打印该路径）。

## 12. 边界

- 绑核与 worker/affinity 最终方案归 `cpu-affinity-optimization`，本 playbook 只给"按 vCPU 倍数扫描"的约束。
- `ssl_engine` 以外的应用层参数（并发、超时、缓冲）归 `application-config-optimization`，不要与其同轮生效。
- 设备规格/算力不足时转 `hardware-upgrade-analysis`。
